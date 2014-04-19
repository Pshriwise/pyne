from warnings import warn

warn(__name__ + " is not yet V&V compliant.", ImportWarning)

import os


if os.name == 'nt':
    p = os.environ['PATH'].split(';')
    lib = os.path.join(os.path.split(__file__)[0], 'lib')
    os.environ['PATH'] = ";".join([lib] + p)
    
from .pyne_config import *
