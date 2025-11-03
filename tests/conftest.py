# Shared pytest configuration for tests
# Intentionally left minimal. Avoid altering global backoff settings so tests
# exercise real control flow; individual tests patch time.sleep to keep runs fast.
