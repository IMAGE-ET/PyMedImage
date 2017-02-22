"""cluster.py

implementation of clustering algorithms and helpers for working with rttypes"""
import os
import logging
import time
import pickle
from sklearn.cluster import KMeans, AgglomerativeClustering
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import SparsePCA as sklearnPCA
import scipy.cluster.hierarchy as sch
import numpy as np
from .misc import indent, g_indents, generate_heatmap_label
from .rttypes import MaskableVolume
from .multiprocess_manager import MultiprocessManager
from . import quantization

# initialize module logger
logger = logging.getLogger(__name__)

# module level variables
GLOBAL = {'scipy_hcluster_valid_methods': ['ward', 'single', 'complete', 'average', 'weighted',
                                           'centroid', 'median'],
          'scipy_hcluster_valid_metrics': ['euclidean', 'cityblock', 'minkowski', 'sqeuclidean',
                                           'seuclidean', 'cosine']
          }

def create_pruned_vector(volume, roi):
    """takes a BaseVolume and creates a vector of intensities where all masked voxels are excluded

    Args:
        volume -- BaseVolume containing feature voxel intensities and valid frameofreference
        roi    -- roi to use when pruning voxels from the resulting feature vector

    Returns:
        numpy 1Darray (vector) where voxel intensities are only included where roi mask is not 0
    """
    logger.debug('pre-pruning voxel count: {!s}'.format(np.prod(volume.frameofreference.size)))
    # no roi defined
    if (not roi):
        pruned_vector = volume.vectorize()

    else:
        # roi defined - actually need to prune based on roi densemask
        (size_x, size_y, size_z) = volume.frameofreference.size
        densemaskvolume = roi.makeDenseMask(volume.frameofreference)
        pruned_vector = []
        for d in range(size_z):
            for r in range(size_y):
                for c in range(size_x):
                    if (densemaskvolume.array[d, r, c] == 1):
                        pruned_vector.append(volume.array[d, r, c])

        # convert to np.array/vector
        pruned_vector = np.array(pruned_vector)

    # convert array to a vector with dims (N, 1) so concatenation can be performed
    logger.debug('post-pruning voxel count: {!s}'.format(pruned_vector.shape[0]))
    return pruned_vector.reshape((-1, 1))

def expand_pruned_vector(pruned_vector, roi, frameofreference, fill_value=-1):
    """takes a sparse vector of voxel intensities and expands into a dense MaskableVolume, filling masked
    voxel locations with fill_value
    performs opposite function of create_pruned_vector

    Args:
        pruned_vector    -- numpy 1Darray containing voxel intensities only for those positions where roi mask
                            evaluates to 1
        roi              -- roi to use when adding voxels into the MaskableVolume
        frameofreference -- informs the roi about how to create dense binary mask necessary for expansion

    Returns:
        MaskableVolume of the same size as frameofreference specified
    """
    logger.debug('pre-expansion voxel count: {!s}'.format(pruned_vector.shape[0]))
    # no roi defined
    if (not roi):
        return MaskableVolume().fromArray(pruned_vector, frameofreference)

    # roi defined - actually need to expand based on roi densemask
    (size_x, size_y, size_z) = frameofreference.size
    densemaskvolume = roi.makeDenseMask(frameofreference)
    expanded_vector = []
    pruned_idx = 0
    for d in range(size_z):
        for r in range(size_y):
            for c in range(size_x):
                if (densemaskvolume.array[d, r, c] == 1):
                    expanded_vector.append(pruned_vector[pruned_idx])
                    pruned_idx += 1
                else:
                    expanded_vector.append(fill_value)

    # convert to np.array/vector
    expanded_vector = np.array(expanded_vector)
    logger.debug('post-expansion voxel count: {!s}'.format(expanded_vector.shape[0]))

    # convert to MaskableVolume
    return MaskableVolume().fromArray(expanded_vector, frameofreference)


def create_feature_matrix(feature_volumes, roi=None, PCA=False):
    """takes a list of feature BaseVolumes and combines them into a numpy ndarray of N rows and D features

    where N is the number of samples in each feature vector (voxels in the image) and D is the number of
    feature vectors stored in the "feature_volumes" list.

    Args:
        feature_volumes    --  python list of BaseVolumes that are aligned
    Returns:
        NDArray     --  numpy ndarray with N rows and D columns where N is the number of voxels in the
                        aligned images (in depth-row major order) and D is the number of feature vectors
                        in the list (len(feature_volumes))
    """
    if len(feature_volumes) <= 0:
        logger.warning(indent('no features supplied. skipping', g_indents[1]))
        return None
    else:
        if (roi):
            # use roi.frameofreference as common shape
            frameofreference = roi.frameofreference
        else:
            # find highest resolution volume and use as FrameOfReference
            highest_res_volume = feature_volumes[0]
            highest_res = np.product(highest_res_volume.frameofreference.spacing)
            for volume in feature_volumes[1:]:
                res = np.product(volume.frameofreference.spacing)
                if (res < highest_res):
                    highest_res_volume = volume
                    highest_res = res
            # assign highest res FOR as common shape
            frameofreference = highest_res_volume.frameofreference
            #OVERRIDE for COMMON RESOLUTION
            frameofreference.spacing = (1, 1, 1)

        # take the selected FORs shape to be the reference
        ref_shape = frameofreference.size[::-1]  # reverses tuple from (x,y,z) to (z,y,x)
        logger.debug('Common Shape (z,y,x): ({:d}, {:d}, {:d})'.format(*ref_shape))

        # create list of commonly shaped feature vectors
        conformed_feature_list = []
        dense_feature_list = []
        feature_column_labels = []
        for i, vol in enumerate(feature_volumes):
            # check for invalid feature
            if (vol is None):
                logger.debug('empty (None) feature provided at index {:d}, removing and continuing'.format(i))
                continue

            # conform feature volumes and add to list
            conformed_volume = vol.conformTo(frameofreference)

            if (conformed_volume.array.shape != ref_shape):
                logger.warning(indent('shape mismatch. ref={ref:s} != feature[{num:d}]={shape:s}.'
                                      ' removing and continuing'.format(ref=str(ref_shape),
                                                                        num=i,
                                                                        shape=str(conformed_volume.array.shape)),
                                      g_indents[1]))
                continue
            else:
                # concatenate, need to make feat.array a 2d vector
                pruned_feature_vector = create_pruned_vector(conformed_volume, roi)
                conformed_feature_list.append(pruned_feature_vector)
                dense_feature_list.append(conformed_volume.vectorize(roi).reshape((-1, 1)))
                # label generator
                label = generate_heatmap_label(conformed_volume)
                feature_column_labels.append(label)

        # combine accepted features into array of shape (nSamples, nFeatures)
        pruned_feature_array = np.nan_to_num(np.concatenate(conformed_feature_list, axis=1))
        # create expanded/dense version for pickling and using in hierarchical clustering
        dense_feature_array = np.nan_to_num(np.concatenate(dense_feature_list, axis=1))

        # dimensionality reduction
        if PCA:
            pca = sklearnPCA()  # sparsePCA
            # pca = sklearnPCA(whiten=True, n_components=None)  # standard PCA
            nfeats = pruned_feature_array.shape[1]
            pruned_feature_array = pca.fit_transform(pruned_feature_array)
            logger.debug('pca: keeping {:d} of {:d} components'.format(pca.n_components, nfeats))

        logger.debug(indent('combined {n:d} features into pruned array of shape: {shape:s}'.format(
            n=pruned_feature_array.shape[1],
            shape=str(pruned_feature_array.shape))
            , g_indents[1]))
        return (pruned_feature_array, frameofreference, dense_feature_array, feature_column_labels)


def cluster_kmeans(feature_matrix, nclusters=10, eps=1e-4, njobs=-2):
    """take input feature array of N rows and D columns and perform standard kmeans clustering using \
            sklearn kmeans library

    Args:
        feature_matrix -- numpy array of N rows and D columns where N is the number of voxels in the
                            volume and D is the number of features.

    Optional Args:
        nclusters      -- number of clusters
        eps            -- epsilon convergence criteria
    Returns:
        imvector of cluster assignments from 0 to k-1 aligned to the BaseVolumes of feature_matrix
    """
    # check inputs
    if not isinstance(feature_matrix, np.ndarray):
        logger.warning(indent('a proper numpy ndarray was not provided. skipping.', g_indents[1]))
        logger.warning(indent(str(type(feature_matrix)) + str(type(np.ndarray)), g_indents[1]))
        return None
    if (nclusters<=1):
        logger.exception(indent('k must be >1', g_indents[1]))
        raise ValueError

    # Preprocessing - normalization
    normalizer = StandardScaler()
    normalized_feature_matrix = normalizer.fit_transform(feature_matrix)

    # create estimator obj
    km = KMeans(n_clusters=nclusters,
                max_iter=300,
                n_init=10,
                init='k-means++',
                precompute_distances=True,
                tol=eps,
                n_jobs=njobs
                )
    km.fit(normalized_feature_matrix)
    logger.debug(indent('#iters: {:d}'.format(km.n_iter_), g_indents[1]))
    logger.debug(indent('score: {score:0.4f}'.format(score=km.score(normalized_feature_matrix)), g_indents[1]))
    return km.predict(normalized_feature_matrix)

def cluster_hierarchical_sklearn(feature_matrix, nclusters=3, affinity='euclidean', linkage='ward'):
    """take input feature array of N rows and D columns and perform agglomerative hierarchical clustering \
            using the standard sklearn agglomerative clustring library

    Args:
        feature_matrix -- numpy array of N rows and D columns where N is the number of voxels in the
                            volume and D is the number of features.

    Optional Args:
        nclusters      -- number of clusters to find
        affinity       -- metric used to compute linkage ['euclidean', 'l1', 'l2', 'manhattan']
        linkage        -- criterion to use for cluster merging ['ward', 'complete', 'average']
    """
    # check inputs
    if not isinstance(feature_matrix, np.ndarray):
        logger.exception(indent('a proper numpy ndarray was not provided. {:s} != {:s}'.format(
            str(type(feature_matrix)),
            str(type(np.ndarray))
        ), g_indents[1]))
        raise TypeError

    # sanitize string inputs
    linkage = linkage.lower()
    affinity = affinity.lower()

    # Preprocessing - normalization
    normalizer = StandardScaler()
    normalized_feature_matrix = normalizer.fit_transform(feature_matrix)

    # determine valid parameters
    valid_linkage = ['ward', 'complete', 'maximum', 'average']
    if (linkage not in valid_linkage):
        logger.exception('linkage must be one of {:s}'.format(str(valid_linkage)))
        raise ValueError(str)
    if (linkage is 'maximum'):
        linkage = 'complete'

    valid_affinity = ['l1', 'l2', 'manhattan', 'cosine', 'euclidean']
    if (affinity not in valid_affinity):
        logger.exception('affinity must be one of {:s}'.format(str(valid_affinity)))
        raise ValueError(str)

    if (linkage is 'ward'):
        # must use euclidean distance
        affinity = 'euclidean'

    conn_matrix = None

    # create estimator obj
    agg = AgglomerativeClustering(n_clusters=nclusters,
                                  connectivity=conn_matrix,
                                  affinity=affinity,
                                  compute_full_tree=True,
                                  linkage=linkage,
                                  pooling_func=np.mean
                                  )

    # perform fit and estimation
    prediction = agg.fit_predict(normalized_feature_matrix)
    logger.debug(indent('#leaves: {:d}'.format(agg.n_leaves_), g_indents[1]))
    logger.debug(indent('#components: {:d}'.format(agg.n_components_), g_indents[1]))
    return (prediction, agg)

def cluster_hierarchical_scipy(feature_matrix, nclusters=3, metric='euclidean', method='ward'):
    """take input feature array of N rows and D columns and perform agglomerative hierarchical clustering \
            using the standard sklearn agglomerative clustring library

    Args:
        feature_matrix -- numpy array of N rows and D columns where N is the number of voxels in the
                            volume and D is the number of features.

    Optional Args:
        nclusters     -- number of clusters to find
        metric        -- metric used to compute linkage ['euclidean', 'l1', 'l2', 'manhattan']
        method        -- criterion to use for cluster merging ['ward', 'complete', 'average']
    """
    # check inputs
    if not isinstance(feature_matrix, np.ndarray):
        logger.exception(indent('a proper numpy ndarray was not provided. {!s} != {!s}'.format(
            type(feature_matrix),
            type(np.ndarray)
        ), g_indents[1]))
        raise TypeError

    # sanitize string inputs
    method = method.lower()
    metric = metric.lower()

    # Preprocessing - normalization
    normalizer = StandardScaler()
    normalized_feature_matrix = normalizer.fit_transform(feature_matrix)

    # determine valid parameters
    valid_method = GLOBAL['scipy_hcluster_valid_methods']
    if (method not in valid_method):
        logger.exception('method must be one of {!s}'.format(valid_method))
        raise ValueError(str)
    if (method is 'maximum'):
        method = 'complete'

    valid_metric = GLOBAL['scipy_hcluster_valid_metrics']
    if (metric not in valid_metric):
        logger.exception('metric must be one of {!s}'.format(valid_metric))
        raise ValueError(str)

    if (method is 'ward'):
        # must use euclidean distance
        metric = 'euclidean'

    # perform fit and estimation
    linkage_matrix = sch.linkage(normalized_feature_matrix, method, metric)
    prediction = sch.fcluster(linkage_matrix, nclusters, criterion='maxclust')
    return (prediction, linkage_matrix)


def DOICluster(doi_list, local_feature_defs, nclusters=20, recluster=False):
    """single/multi-doi, multi-feature sub-unit that can be multithreaded and called by a pool of workers

    Args:
        doi (str): string identifier unique to each patient/doi
        doi (list): collection of doi's to be clustered together (usually multimodal same patient)
        local_feature_defs (list<LocalFeatureDefinition>): contains information for identifying features
            to cluster

    Returns:
        int: status code
    """
    if not isinstance(doi_list, list):
        doi_list = [doi_list]

    feature_volume_list = []
    for doi in doi_list:
        # generate doi specific paths
        p_doi_features = doi.getFeaturesPath()
        p_doi_clusters = os.path.join(doi.p_CLUSTERS, doi.doi)
        p_clusters_l1_pickle = doi.getClusterL1PicklePath()

        # check for existing cluster
        reclustered = False
        if (os.path.exists(p_clusters_l1_pickle)):
            if not recluster:
                return 10
            else:
                reclustered = True

        # load dicom data
        image_vol = doi.getImageVolume()
        # print('image_vol_modality: {!s}'.format(image_vol.modality))
        # print('image_vol_feature_label: {!s}'.format(image_vol.feature_label))
        roi = doi.getROI()
        if (not image_vol or not roi):
            # print('missing ct or roi. skipping.')
            return 1
        feature_volume_list.append(image_vol)

        # load feature volumes
        for feature_def in local_feature_defs:
            # force stat based GLCM quantization if not CT image
            trimmed_feature_def = quantization.enforceGLCMQuantizationMode(feature_def, image_vol.modality)

            # import features and append to feature volume list
            matches = trimmed_feature_def.findFiles(p_doi_features)
            if not matches:
                message = 'Feature file couldn\'t be found: {!s}'.format(trimmed_feature_def.generateFilename())
                logger.error(message)
                return 1

            feature_volume_list.append(MaskableVolume().fromPickle(matches[0]))

    # create feature matrix for clustering from feature volume list (pruning is handled automatically)
    (pruned_feature_array, frameofreference, \
        full_feature_array, feat_column_labels) = create_feature_matrix(feature_volume_list, roi, PCA=False)

    # Cluster and create cluster volume then pickle it
    pruned_cluster_vector = cluster_kmeans(pruned_feature_array, nclusters, njobs=1)
    dense_cluster_volume = expand_pruned_vector(pruned_cluster_vector, roi, frameofreference)
    os.makedirs(p_doi_clusters, exist_ok=True)
    dense_cluster_volume.toPickle(p_clusters_l1_pickle)

    # store feature matrix in pickle as numpy ndarray
    # package full_feature_array into dict with labeling of the featues associated with each column
    # and each row corresponding to each row of the dense_cluster_volume in flattened form
    featpickle_dict = {'feature_matrix': full_feature_array,
                       'labels':         feat_column_labels}
    with open(doi.getClusterL1FeaturesPicklePath(), mode='wb') as f:
        pickle.dump(featpickle_dict, f)  # dense form

    if reclustered:
        return 11
    else:
        return 0

def worker_DOICluster(args_tuple):
    """handles worker pool logging and argument unpacking"""
    time_start = time.time()
    try:
        (doi, local_feature_def, nclusters) = args_tuple[:3]
        result_code = DOICluster(*args_tuple, recluster=True)

        if (result_code == 0):
            result_string = 'success'
        elif (result_code == 1):
            result_string = 'missing data'
        elif (result_code == 10):
            result_string = 'skipped'
        elif (result_code == 11):
            result_string = 'recalc'
        else:
            # unknown result
            result_code = -1
            result_string = 'unknown'

    except Exception as e:
        result_code = 2
        result_string = 'exception'
        logger.error('{!s}'.format(e))

    time_end = time.time()
    job_time_string = time.strftime('%H:%M:%S', time.gmtime(time_end-time_start))

    return (result_code, result_string, job_time_string, doi)

def logstringgenerator_DOICluster(worker_results):
    (result_code, result_string, job_time_string, doi) = worker_results
    if isinstance(doi, list): doi = doi[0]
    log_string = '[{string:12s}:{code:2d}]: {doi!s:9s}  {time!s}'.format(
        string  = result_string,
        code    = result_code,
        doi     = doi,
        time    = job_time_string
    )
    return log_string

def multiprocessDOICluster(doi_list, feature_defs, nclusters=20, processes=16, notify=True):
    argmap = []
    for doi in doi_list:
        argmap.append((doi, feature_defs, nclusters))

    manager = MultiprocessManager('Level 1 Clustering')
    manager.registerWorkerFunction(worker_DOICluster)
    manager.registerLogStringGenerator(logstringgenerator_DOICluster)
    manager.execute(argmap, processes=processes, notify=notify)
