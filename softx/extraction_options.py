"""Allow individual evidence rules to be measured in local benchmarks."""
import os


def enabled(rule):
    selected = os.getenv("SOFTX_EXTRACTION_EXPERIMENTS")
    return selected is None or rule in selected.split(",")
