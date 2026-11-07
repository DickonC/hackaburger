import os
import tempfile

# Must be set before app.core is imported
os.environ["API_KEY"] = "test-key"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
