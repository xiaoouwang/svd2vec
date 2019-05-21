
import bz2
import heapq
import pickle
import numpy as np
import pandas as pd
import multiprocessing

from scipy.sparse import csc_matrix
from scipy.sparse.linalg import svds
from scipy.spatial.distance import cosine
from joblib import Parallel, delayed
from collections import OrderedDict, Counter
from operator import itemgetter
from tqdm import tqdm

from .utils import Utils
from .window import WindowWeights
from .temporary_array import TemporaryArray

class svd2vec:

    WINDOW_WEIGHT_HARMONIC = 0
    WINDOW_WEIGHT_WORD2VEC = 1

    NRM_SCHEME_NONE   = "none"
    NRM_SCHEME_ROW    = "row"
    NRM_SCHEME_COLUMN = "column"
    NRM_SCHEME_BOTH   = "both"
    NRM_SCHEMES = [NRM_SCHEME_NONE, NRM_SCHEME_ROW, NRM_SCHEME_COLUMN, NRM_SCHEME_BOTH]

    MAX_CPU_CORES = -1

    def __init__(self,
                 documents,
                 size=150,
                 min_count=2,
                 window=10,
                 dyn_window_weight=WINDOW_WEIGHT_WORD2VEC,
                 cds_alpha=0.75,
                 neg_k_shift=5,
                 eig_p_weight=0,
                 nrm_type=NRM_SCHEME_ROW,
                 sub_threshold=1e-5,
                 verbose=True,
                 workers=MAX_CPU_CORES):

        # -------------
        # args checking
        # -------------

        # dyn weight
        if dyn_window_weight == svd2vec.WINDOW_WEIGHT_HARMONIC:
            window_weighter = WindowWeights.weight_harmonic
        elif dyn_window_weight == svd2vec.WINDOW_WEIGHT_WORD2VEC:
            window_weighter = WindowWeights.weight_word2vec
        else:
            raise ValueError(dyn_window_weight + " not implemented as a weighter")

        # window type
        if isinstance(window, int):
            window = WindowWeights.create_window(left=window, right=window, weighter=window_weighter)
        elif isinstance(window, tuple) and len(window) == 2 and all(map(lambda e: isinstance(e, int), window)):
            window = WindowWeights.create_window(left=window[0], right=window[1], weighter=window_weighter)
        else:
            raise ValueError("'" + window + "' not implemented as a window yielder")

        # normalization type
        if nrm_type not in svd2vec.NRM_SCHEMES:
            raise ValueError("'" + nrm_type + "' cannot be used in as a normalization method")

        # workers
        if isinstance(workers, int):
            if workers < 1:
                workers = multiprocessing.cpu_count()
        else:
            raise ValueError("'" + workers + "' is not a valid cpu count")

        # -----------
        # args saving
        # -----------

        self.workers       = workers
        self.min_count     = min_count
        self.size          = size
        self.window        = window
        self.cds_alpha     = cds_alpha
        self.sub_threshold = sub_threshold
        self.neg_k_shift   = neg_k_shift
        self.eig_p_weight  = eig_p_weight
        self.nrm_type      = nrm_type
        self.verbose       = verbose
        self.bar_offset    = 0

        # --------------------
        # document preparation
        # --------------------
        self.build_vocabulary(documents)
        self.subsampling()
        self.weighted_count_matrix_file = self.skipgram_weighted_count_matrix()
        self.clean_instance_variables()

        # ---------------
        # pmi computation
        # ---------------
        self.pmi = self.sparse_pmi_matrix(self.sppmi_matrix(self.pmi_matrix()))

        # ---------------
        # svd computation
        # ---------------

        self.svd_w, self.svd_c = self.svd()

        # -------
        # closing
        # -------
        # weighted_count_matrix_file was not a simple numpy matrix at a path to
        # a memmap of a numpy matrix. Now we can remove the temporary file
        self.weighted_count_matrix_file.close()

    #####
    # Building informations matrices and variables to be used later
    #####

    def build_vocabulary(self, documents):
        bar = self.bar(desc="vocabulary building", total=8)
        self.documents = documents
        bar.update()
        self.all_words = Utils.flatten(self.documents)
        bar.update()
        self.d_size    = len(self.all_words)
        bar.update()
        self.d_size_cds_power = np.power(self.d_size, self.cds_alpha)
        bar.update()

        self.terms_counts = Counter(self.all_words)
        bar.update()
        self.terms_counts_cds_powered = {word: np.power(self.terms_counts[word], self.cds_alpha) for word in self.terms_counts}
        bar.update()

        self.vocabulary     = OrderedDict([(w, i) for i, (w, c) in enumerate(self.terms_counts.most_common())])
        bar.update()
        self.vocabulary_len = len(self.vocabulary)
        bar.update()
        bar.close()

    def subsampling(self):
        new_docs = []
        for document in self.bar(self.documents, "document subsampling"):
            new_words = []
            for word in self.bar(document, "word subsampling", offset=1):
                if self.terms_counts[word] < self.min_count:
                    continue
                word_frequency = 1.0 * self.terms_counts[word] / self.d_size
                prob = 1 - np.sqrt(self.sub_threshold / word_frequency)
                if not Utils.random_decision(prob):
                    # we keep the word
                    new_words.append(word)
            new_docs.append(new_words)
        self.build_vocabulary(new_docs)

    def bar(self, yielder=None, desc=None, total=None, offset=0):
        disable = not self.verbose
        return tqdm(
            iterable=yielder,
            desc=desc,
            leave=False,
            total=total,
            disable=disable,
            position=offset,
            bar_format="{desc: <30} {percentage:3.0f}%  {bar}")

    def skipgram_weighted_count_matrix(self):
        file = TemporaryArray((self.vocabulary_len, self.vocabulary_len), np.dtype('float16'))
        matrix = file.load(erase=True)

        for document in self.bar(self.documents, "co-occurence counting"):
            for word, context, weight in self.bar(self.window(document), "document co-occurence counting", total=self.vocabulary_len * self.vocabulary_len, offset=1):
                i_word    = self.vocabulary[word]
                i_context = self.vocabulary[context]
                matrix[i_word, i_context] += weight

        matrix.flush()
        del matrix
        return file

    def clean_instance_variables(self):
        # these two instances variables uses too much RAM, and it's not longer
        # useful
        delattr(self, "all_words")
        delattr(self, "documents")

        # we do not need the window function anymore, plus keeping it as an
        # instance variable will stop us from using joblib parallelisation
        # because this can not be saved as a pickle object
        delattr(self, "window")

    def pmi_matrix(self):
        # pointwise mutal information

        slices = Utils.split(list(self.vocabulary), self.workers)
        pmi_list = Parallel(n_jobs=self.workers)(delayed(self.pmi_parallized)(slice, i) for i, slice in enumerate(slices) if slice != [])
        pmi = np.concatenate(pmi_list, axis=0)

        if self.verbose:
            print("")

        return pmi

    def pmi_parallized(self, slice, i):
        # returns a small matrix corresponding to the slice of words given (rows)
        pmi = np.zeros((len(slice), self.vocabulary_len))
        self.weighted_count_matrix_offset = self.vocabulary[slice[0]]
        self.weighted_count_matrix = self.weighted_count_matrix_file.load(size=len(slice), start=self.weighted_count_matrix_offset)

        name = "pmi " + str(i + 1) + " / " + str(self.workers)
        for i_word, word in enumerate(self.bar(slice, desc=name, offset=i)):
            for context in self.vocabulary:
                i_context = self.vocabulary[context]
                pmi[i_word, i_context] = self.pmi(word, context)

        del self.weighted_count_matrix
        return pmi

    def ppmi_matrix(self, pmi):
        # positive pointwise mutal information
        zero = np.zeros(pmi.shape)
        return np.maximum(pmi, zero)

    def sppmi_matrix(self, pmi):
        # shifted positive pointwise mutal information
        spmi = pmi - np.log(self.neg_k_shift)
        return self.ppmi_matrix(spmi)

    def sparse_pmi_matrix(self, pmi):
        sparsed = csc_matrix(pmi)
        return sparsed

    def svd(self):
        bar = self.bar(desc="singular value decomposition", total=5)
        modified_k = min(self.size, self.pmi.shape[0] - 1)
        bar.update()
        u, s, v = svds(self.pmi, k=modified_k)
        bar.update()

        w_svd_p = u * np.power(s, self.eig_p_weight)
        bar.update()
        c_svd   = v.T
        bar.update()

        w_svd_p = self.normalize(w_svd_p, self.nrm_type)
        bar.update()
        bar.close()

        return w_svd_p, c_svd

    def normalize(self, matrix, nrm_type):
        if nrm_type == svd2vec.NRM_SCHEME_NONE:
            return matrix
        if nrm_type == svd2vec.NRM_SCHEME_ROW:
            return matrix / np.linalg.norm(matrix, axis=0, keepdims=True)
        if nrm_type == svd2vec.NRM_SCHEME_COLUMN:
            return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
        if nrm_type == svd2vec.NRM_SCHEME_BOTH:
            raise NotImplementedError("Normalization NRM_SCHEME_BOTH not yet implemented")
        raise ValueError("Normalization '" + nrm_type + "' error")

    #####
    # I/O
    #####

    def save(self, path):
        with bz2.open(path, "wb") as file:
            pickle.dump(self, file)

    def load(path):
        with bz2.open(path, "rb") as file:
            return pickle.load(file)

    #####
    # Getting informations
    #####

    def weight_count_term(self, term, cds_power=False):
        if cds_power:
            count_term = self.terms_counts_cds_powered[term]
        else:
            count_term = self.terms_counts[term]
        return count_term

    def weight_count_term_term(self, t1, t2):
        i_t1 = self.vocabulary[t1] - self.weighted_count_matrix_offset
        i_t2 = self.vocabulary[t2]
        weighted_count = self.weighted_count_matrix[i_t1, i_t2]
        return weighted_count

    def pmi(self, word, context):
        n_wc = self.weight_count_term_term(word, context)
        n_w  = self.weight_count_term(word)
        n_c_powered = self.weight_count_term(context, cds_power=True)

        p_wc = n_wc / self.d_size
        p_w  = n_w  / self.d_size
        p_c  = n_c_powered / self.d_size_cds_power

        frac = p_wc / (p_w * p_c)

        if frac == 0:
            return -np.inf
        return np.log(frac)

    def cosine_similarity(self, wx, cx, wy, cy):
        # compute the cosine similarity of x (word x and context x) and y (word
        # y and context y)
        top = np.dot(wx + cx, wy + cy)
        bot = np.sqrt(np.dot(wx + cx, wx + cx)) * np.sqrt(np.dot(wy + cy, wy + cy))
        return top / bot

    def similarity(self, x, y):
        # Returns the similarity of the two words x and y
        wx, cx = self.vectors(x)
        wy, cy = self.vectors(y)
        sim = self.cosine_similarity(wx, cx, wy, cy)
        return sim

    def most_similar(self, positive=[], negative=[], topn=10):
        # Output the most similar words for the given positive and negative
        # words. topn limits the number of output words
        if not isinstance(positive, list) or not isinstance(negative, list):
            raise ValueError("Positive and Negative should be a list of words inside the vocabulary")
        if positive == [] and negative == []:
            raise ValueError("Cannot get the most similar words without any positive or negative words")

        positives = [self.vectors(x) for x in positive]
        negatives = [self.vectors(x) for x in negative]

        first_w, first_c = positives[0] if positive else negatives[0]

        current_w = np.zeros(first_w.shape)
        current_c = np.zeros(first_c.shape)

        for positive_w, positive_c in positives:
            current_w += positive_w
            current_c += positive_c
        for negative_w, negative_c in negatives:
            current_w -= negative_w
            current_c -= negative_c

        not_to_calc_similiarity = set(positive).union(set(negative))

        similiarities = {}
        for word in self.vocabulary:
            if word in not_to_calc_similiarity:
                continue
            w, c = self.vectors(word)
            sim  = self.cosine_similarity(current_w, current_c, w, c)
            similiarities[word] = sim

        most_similar = heapq.nlargest(topn, similiarities.items(), key=itemgetter(1))
        return most_similar

    def analogy(self, exampleA, answerA, exampleB):
        # returns answerB, ie the answer to the question
        # exampleA is to answerA as exampleB is to answerB
        return self.most_similar(positive=[exampleB, answerA], negative=[exampleA])

    def vectors(self, word):
        if word in self.vocabulary:
            i_word = self.vocabulary[word]
            w = self.svd_w[i_word]
            c = self.svd_c[i_word]
            return w, c
        else:
            raise ValueError("Word '" + word + "' not in the vocabulary")

    #####
    # Debug
    #####

    def display_matrix(self, matrix, vocabulary=None):
        if vocabulary is None:
            vocabulary = self.vocabulary
        z = {a: e for e, a in vocabulary.items()}
        v = [z[i] for i in sorted(vocabulary.values())]
        df = pd.DataFrame(matrix.toarray(), columns=v, index=v)
        df = df.applymap(lambda x: '{:4.2f}'.format(x) if x != 0 else "")
        print(df)

    def display_similarity(self, word1, word2):
        sim = self.similarity(word1, word2)
        print(word1, " & ", word2, sim)

    def display_most_similar(self, positive=[], negative=[]):
        sims = self.most_similar(positive=positive, negative=negative)
        print(positive, negative, sims)
