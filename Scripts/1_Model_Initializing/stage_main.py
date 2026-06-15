import pandas as pd
import numpy as np
import re
from typing import cast
from pathlib import Path
from string import ascii_lowercase
from datetime import date, datetime, timedelta

# Parameters
BEAM_HEIGHT = 16
MAX_TOTAL_HEIGHT = 770
CLEARANCE = 5
MIN_BEAM_COUNT = 3
# BUFFER_LOCATION_COUNT = 
CURRENT_SKU_COUNT = 843
CURRENT_LOCATION_COUNT = 1152
CURRENT_NORMAL_BEAM_COUNT = 251
CURRENT_LARGE_BEAM_COUNT = 46
CURRENT_SMALL_BEAM_COUNT = 10
CURRENT_GRID_COUNT = 949
BIN_COUNT = {4,5,6,7}
