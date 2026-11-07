import os
import tempfile

# Must be set before app.core is imported
os.environ["API_KEY"] = "test-key"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["MODELS_DIR"] = tempfile.mkdtemp()
os.environ["HEATMAP_TICK_S"] = "0"  # tests call heatmap.tick() themselves


def pytest_configure(config):
    # occupancy_forecaster uses .dayofweek, deprecated (not yet removed) in pandas 3
    config.addinivalue_line("filterwarnings", "ignore:.*dayofweek is deprecated")
