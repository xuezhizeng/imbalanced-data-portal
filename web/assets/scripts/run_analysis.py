import ast
import json
import os
import random
import re
import sys
import timeit

import numpy as np
import pandas as pd
import pymysql
from scipy import interp, stats
from sklearn import decomposition, model_selection, preprocessing
from sklearn.ensemble import ExtraTreesClassifier, GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression, SGDClassifier
from sklearn.metrics import *
from sklearn.naive_bayes import GaussianNB
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import label_binarize
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier


#######################
# Parameter Retrieval #
#######################

start = timeit.default_timer()

analysis_id = sys.argv[1]
#current_dir = sys.argv[2]
current_dir = os.path.dirname(os.path.realpath(__file__))

# Connect to the database.
db = pymysql.connect(host='localhost', user='root', passwd='', db='symfony')
cursor = db.cursor()

# Find the current analysis object in the results database.
cursor.execute(
    "SELECT dataset_id, preprocessing_params, params, model_id FROM ode_results WHERE id=" + analysis_id)
analysis = cursor.fetchone()

# Find the dataset to be used by the current analysis.
cursor.execute("SELECT * FROM ode_dataset WHERE id=" + str(analysis[0]))
dataset = cursor.fetchone()


#################################
# Data Pre-processing Functions #
#################################

preprocessing_params = json.loads(analysis[1])


def undersample(X, y, ix, subsampling_rate=1.0):
    """ Data undersampling.

    This function takes in a list or array indexes that will be used for
    training and it performs subsampling in the majority class (c == 0) to
    enforce a certain ratio between the two classes.

    Parameters
    ----------
    X : np.ndarray
        The entire dataset as a ndarray
    y : np.ndarray
        The labels
    ix : np.ndarray
        The array indexes for the instances that will be used for training.
    subsampling_rate : float
        The desired percentage of majority instances in the subsample.

    Returns
    --------
    np.ndarray
        The new list of array indexes to be used for training
    """
    # Determine indexes of instances that belong to classes 0 and 1.
    indexes_0 = [item for item in ix if y[item] == 0]
    indexes_1 = [item for item in ix if y[item] == 1]

    # Determine size of the new majority class set.
    sample_length = int(len(indexes_0) * subsampling_rate)
    sample_indexes = random.sample(indexes_0, sample_length) + indexes_1

    return sample_indexes


def SMOTE(T, N, k, h=1.0):
    """ Synthetic minority oversampling.

    Returns (N/100) * n_minority_samples synthetic minority samples.

    Parameters
    ----------
    T : array-like, shape = [n_minority_samples, n_features]
        Holds the minority samples
    N : percentage of new synthetic samples:
        n_synthetic_samples = N/100 * n_minority_samples. Can be < 100.
    k : int. Number of nearest neighbors.

    Returns
    -------
    S : Synthetic samples. array,
        shape = [(N/100) * n_minority_samples, n_features].
    """
    from sklearn.neighbors import NearestNeighbors

    n_minority_samples, n_features = T.shape

    if N < 100:
        # create synthetic samples only for a subset of T.
        # TODO: select random minority samples.
        N = 100
        pass

    if (N % 100) != 0:
        raise ValueError("N must be < 100 or multiple of 100.")

    N = N / 100
    n_synthetic_samples = N * n_minority_samples
    S = np.zeros(shape=(n_synthetic_samples, n_features))

    # Learn nearest neighbors.
    neigh = NearestNeighbors(n_neighbors=k)
    neigh.fit(T)

    # Calculate synthetic samples.
    for i in xrange(n_minority_samples):
        nn = neigh.kneighbors(T[i], return_distance=False)
        for n in xrange(N):
            nn_index = random.choice(nn[0])
            # NOTE: nn includes T[i], we don't want to select it.
            while nn_index == i:
                nn_index = random.choice(nn[0])

            dif = T[nn_index] - T[i]
            gap = np.random.uniform(low=0.0, high=h)
            S[n + i * N, :] = T[i, :] + gap * dif[:]

    return S

    
########################################
# Retrieving Dataset and Preprocessing #
########################################

df = pd.read_csv(os.path.join(os.path.dirname(current_dir), 
                 'datasets', str(dataset[8]) + '.csv'))
X = df.drop(df.columns[df.shape[1] - 1], axis=1)
y = preprocessing.LabelEncoder().fit_transform(
    df.iloc[:, df.shape[1] - 1].values)

# Fill in missing values.
if (preprocessing_params['missing_data'] == 'default'):
    X = X.fillna(-1)
elif(preprocessing_params['missing_data'] == 'average'):
    X = X.fillna(X.mean())
elif (preprocessing_params['missing_data'] == 'interpolation'):
    X = X.interpolate()

# Remove outliers.
# TODO: Move this to traning step of K-fold so as to keep test untouched?
if preprocessing_params['outlier_detection']:
    non_outlier_idx = (np.abs(stats.zscore(X)) < 3).all(axis=1)
    X = X[non_outlier_idx]
    y = y[non_outlier_idx]

# Perform standardization.
# TODO: Should this come before encoding?
if preprocessing_params['standardization']:
    X = preprocessing.scale(X)

# Perform normalization.
# TODO: Check to make sure order doesn't matter.
if preprocessing_params['normalization']:
    X = preprocessing.normalize(X, norm=preprocessing_params['norm'])

# Perform binarization.
# TODO: Check to make sure order doesn't matter.
if preprocessing_params['binarization']:
    binarizer = preprocessing.Binarizer(
        threshold=preprocessing_params['binarization_threshold']).fit(X)
    X = binarizer.transform(X)

# Apply PCA.
if preprocessing_params['pca']:
    estimator = decomposition.PCA(
        n_components=preprocessing_params['n_components'])
    X = estimator.fit_transform(X)


######################
# Running Experiment #
######################

# Declare all classifiers (these keys map to the keys in ode_models).
clfs = {
    1: DecisionTreeClassifier(),
    2: ExtraTreesClassifier(),
    3: GaussianNB(),
    4: GradientBoostingClassifier(),
    5: KNeighborsClassifier(),
    6: LogisticRegression(),
    7: RandomForestClassifier(),
    8: SGDClassifier(),
    9: SVC()
}

model_params = ast.literal_eval(analysis[2])

# Needed to avoid "shuffle must be True or False error when using SGDClassifier".
if (analysis[3] == 8):
    model_params['shuffle'] = bool(model_params['shuffle'])

# Select the correct classifier and set user-specified parameters.
if (analysis[2]):
    clf = clfs[analysis[3]].set_params(**model_params)
else:
    clf = clfs[analysis[3]]

mean_tpr = 0.0
mean_fpr = np.linspace(0, 1, 100)

y_test = []
y_prob = []
y_pred = []
indexes = []

if len(np.unique(y)) > 2:
    # Binarize the output.
    y_bin = label_binarize(y, classes=np.unique(y))
    n_classes = y_bin.shape[1]
elif len(np.unique(y)) == 2:
    # Binary output.
    y_bin = y.reshape((-1, 1))
    n_classes = 2
else:
    print("Need 2 or more class values.")

# Run 10-fold cross-validation and compute AUROC.
n_splits = 10
skf = model_selection.StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=0)
for fold, (train_idx, test_idx) in enumerate(skf.split(X, y)):
    if preprocessing_params['undersampling']:
        train_idx = undersample(X, y, train_idx,
                                float(preprocessing_params['undersampling_rate']) / 100)

    X_train_i = X.iloc[train_idx]
    y_train_i = y_bin[train_idx]

    X_test_i = X.iloc[test_idx]
    y_test_i = y_bin[test_idx]
    
    if preprocessing_params['oversampling']:
        minority = X_train_i[np.where(y_train_i == 1)]
        smoted = SMOTE(
            minority, preprocessing_params['undersampling_rate'], 5)
        X_train_i = np.vstack((X_train_i, smoted))
        y_train_i = np.append(y_train_i, np.ones(len(smoted), dtype=np.int32))

    clf.fit(X_train_i, y_train_i)
    y_pred_i = clf.predict(X_test_i)
    y_prob_i = np.array(clf.predict_proba(X_test_i))

    # Compute ROC curve and ROC area for each class.
    fpr = dict()
    tpr = dict()
    pre = dict()
    rec = dict()
    roc_auc = dict()
    pr_auc = dict()
    if n_classes > 2:
        for i in range(n_classes):
            fpr[i], tpr[i], _ = roc_curve(y_test_i[:, i], y_pred_i[:, i])
            pre[i], rec[i], _ = precision_recall_curve(y_test_i[:, i], y_pred_i[:, i])
            roc_auc[i] = auc(fpr[i], tpr[i])
            pr_auc[i] = auc(pre[i], rec[i])

        # Compute micro-average ROC curve and ROC area.
        fpr["micro"], tpr["micro"], _ = roc_curve(y_test_i.ravel(), y_pred_i.ravel())
        roc_auc["micro"] = auc(fpr["micro"], tpr["micro"])

        # Compute micro-average P-R curve and P-R area.
        pre["micro"], rec["micro"], _ = \
            precision_recall_curve(y_test_i.ravel(), y_pred_i.ravel())
        roc_auc["micro"] = auc(fpr["micro"], tpr["micro"])
        pr_auc["micro"] = auc(pre["micro"], rec["micro"])

        # Compute macro-average ROC curve and ROC area.

        # First aggregate all false positive rates.
        all_fpr = np.unique(np.concatenate([fpr[i] for i in range(n_classes)]))

        # Then interpolate all ROC curves at this points.
        mean_tpr = np.zeros_like(all_fpr)
        for i in range(n_classes):
            mean_tpr += interp(all_fpr, fpr[i], tpr[i])

        # Finally average it and compute AUC.
        mean_tpr /= n_classes

        fpr["macro"] = all_fpr
        tpr["macro"] = mean_tpr
        roc_auc["macro"] = auc(fpr["macro"], tpr["macro"])

        # Compute ROC curve and area under the curve.
        #fpr, tpr, thresholds = roc_curve(y_test_i, y_prob_i[:, 1])

        #mean_tpr += interp(mean_fpr, fpr, tpr)
        #mean_tpr[0] = 0.0

        # Define y-values and corresponding predicted probabilities and values.
        y_test = np.concatenate((y_test, y[test_idx]), axis=0)
        y_prob = np.concatenate((y_prob, np.argmax(y_pred_i, axis=1)), axis=0)
        y_pred = np.concatenate((y_pred, np.argmax(y_pred_i, axis=1)), axis=0)
        indexes = np.concatenate((indexes, test_idx), axis=0)
    else:
        # Compute ROC curve and area under the curve.
        fpr["micro"], tpr["micro"], _ = roc_curve(y[test_idx], y_prob_i[:, 1])

        mean_tpr += interp(mean_fpr, fpr["micro"], tpr["micro"])
        mean_tpr[0] = 0.0

        # Define y-values and corresponding predicted probabilities and values.
        y_test = np.concatenate((y_test, y[test_idx]), axis=0)
        y_prob = np.concatenate((y_prob, y_prob_i[:, 1]), axis=0)
        y_pred = np.concatenate((y_pred, y_pred_i), axis=0)
        indexes = np.concatenate((indexes, test_idx), axis=0)

if n_classes == 2:
    # Compute TPR and AUROC
    mean_tpr /= n_splits
    mean_tpr[-1] = 1.0
    auroc = auc(mean_fpr, mean_tpr)

    # Compute precision-recall curve points and area under the PR-curve.
    pre["micro"], rec["micro"], _ = precision_recall_curve(y_test, y_prob)
    roc_auc["micro"] = auc(fpr["micro"], tpr["micro"])
    pr_auc["micro"] = auc(rec["micro"], pre["micro"])

# Store a flag for mispredictions.
errors = np.logical_xor(y_pred, y_test).astype(int)

# Compute overall accuracy.
accuracy = 1 - (np.sum(errors) / float(len(errors)))

# Store x-y coordinates of ROC curve points.
roc_points = ''
for x in zip(fpr["micro"], tpr["micro"]):
    roc_points += ('[' + str('%.3f' % x[0]) + ',' + str('%.3f' % x[1]) + '],')
roc_points = roc_points[:-1]

# Store x-y coordinates of P-R curve points.
prc_points = ''
for x in zip(rec["micro"], pre["micro"]):
    prc_points += ('[' + str('%.3f' % x[0]) + ',' + str('%.3f' % x[1]) + '],')
prc_points = prc_points[:-1]

# Store confusion matrix as a string with comma-separated values.
confusion_matrix = str(confusion_matrix(
    y_test, y_pred).tolist()).replace(']', '').replace('[', '')

# Store a list of the numeric values returned by classification_report().
clf_report = re.sub(
    r'[^\d.]+', ', ', classification_report(y_test, y_pred))[5:-2]

# Compute precision, recall, and F1-score.
precision, recall, f1_score, support = precision_recall_fscore_support(
    y_test, y_pred)

# Limit number of instances saved to the database so report will finish.
# TODO: Extend reporting to an arbitrary number of instances.
LAST_INDEX = 1000 if (len(indexes) > 1000) else len(indexes)

# Sort results by instance number.
sorted_ix = np.argsort(indexes)
indexes = ','.join(str(e) for e in indexes[sorted_ix][:LAST_INDEX].astype(int))
y_test = ','.join(str(e) for e in y_test[sorted_ix][:LAST_INDEX].astype(int))
y_pred = ','.join(str(e) for e in y_pred[sorted_ix][:LAST_INDEX].astype(int))
errors = ','.join(str(e) for e in errors[sorted_ix][:LAST_INDEX]).replace(
    '0', ' ').replace('1', '&#x2717;')
y_prob = ','.join(str(e) for e in np.around(y_prob[sorted_ix][:LAST_INDEX],
                                            decimals=4))


##################################
# Saving Results to the Database #
##################################

report_data = json.dumps({'roc_points': roc_points, 
                          'prc_points': prc_points, 
                          'confusion_matrix': confusion_matrix, 
                          'classification_report': clf_report,
                          'indexes': indexes, 
                          'y_original_values': y_test, 
                          'y_pred': y_pred, 
                          'y_prob': y_prob, 
                          'errors': errors
                          })

# Update the entry in the database to reflect completion.
stop = timeit.default_timer()
cursor.execute("UPDATE ode_results SET finished=1, runtime=" + \
               str(stop - start) + \
               ", aupr=" + str(pr_auc["micro"]) + \
               ", auroc=" + str(roc_auc["micro"]) + \
               ", accuracy=" + str(accuracy) + \
               ", precision_score=" + str(precision[1]) + \
               ", recall_score=" + str(recall[1]) + \
               ", f1_score=" + str(f1_score[1]) + \
               ", report_data=\'" + report_data + \
               "\' WHERE id=" + analysis_id
               )
db.commit()

# Close connection with the database.
cursor.close()
db.close()
