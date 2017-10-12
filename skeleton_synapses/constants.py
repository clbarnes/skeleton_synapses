import logging
import os

DEBUG = False
ALGO_HASH = None  # set to fix algorithm hash
LOG_LEVEL = logging.DEBUG

DEFAULT_ROI_RADIUS = 150

TQDM_KWARGS = {
    'ncols': 50,
}

RESULTS_TIMEOUT_SECONDS = 5*60  # result fetchers time out after 5 minutes

ROOT_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), '..')
