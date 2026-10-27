"""Test setup: real PostgreSQL (meerpad's rule: SQLite would prove nothing about
row locks or JSONB).

Point MEERTEX_TEST_DB at a throwaway database, e.g. the one `make test-db`
creates. Without it the database tests skip.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = Path(tempfile.mkdtemp(prefix="meertex-test-"))
os.environ["DATA_DIR"] = str(_tmp / "data")
os.environ["BASE_URL"] = "http://testserver"
os.environ["SMTP_HOST"] = ""
os.environ["SECRET_KEY"] = "test-secret"
# Compiling is off, also when .env sets COMPILE_URL. The compile tests start
# their own service and switch it on (tests/compile_helpers.py).
os.environ["COMPILE_URL"] = ""
# Snapshots on demand only, unless a test asks for the timers.
os.environ.setdefault("SNAPSHOT_IDLE_SECONDS", "3600")
os.environ.setdefault("SNAPSHOT_MAX_SECONDS", "3600")
# No reminder loop in the test apps. tests/test_reminders.py calls its functions
# directly, with a "now" of its own.
os.environ["REMINDERS_ENABLED"] = "false"
if os.environ.get("MEERTEX_TEST_DB"):
    os.environ["DATABASE_URL"] = os.environ["MEERTEX_TEST_DB"]
else:
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://invalid@127.0.0.1:1/none")
