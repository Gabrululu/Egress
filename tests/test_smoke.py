def test_packages_import():
    import copilot  # noqa: F401
    import scanner

    assert scanner.__version__
