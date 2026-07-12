import os
import sys

# make `import proxy` and `import stub_upstream` / `import helpers` work
# regardless of where pytest is invoked from
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
