import sys
import os

# Add the workspace root to sys.path so that `app.*` and `sentinel.*`
# are importable from any pytest invocation regardless of working directory.
sys.path.insert(0, os.path.dirname(__file__))
