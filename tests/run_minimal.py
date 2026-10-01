"""没有 pytest 时的最小运行器：python tests/run_minimal.py"""
import contextlib, importlib, sys, traceback, types

try:
    import pytest  # noqa
except ImportError:
    shim = types.ModuleType("pytest")
    shim.fixture = lambda f: f
    @contextlib.contextmanager
    def raises(exc):
        try:
            yield
        except exc:
            return
        raise AssertionError(f"应当抛出 {exc.__name__}")
    shim.raises = raises
    sys.modules["pytest"] = shim

sys.path.insert(0, ".")
mod = importlib.import_module(sys.argv[1] if len(sys.argv) > 1 else "tests.test_engine")
fails = 0
for name, fn in sorted(vars(mod).items()):
    if name.startswith("test_") and callable(fn):
        try:
            fn(mod.env()) if fn.__code__.co_argcount else fn()
            print("PASS", name)
        except Exception:
            fails += 1
            print("FAIL", name); traceback.print_exc()
print("\n%d failed" % fails if fails else "\nall passed")
sys.exit(1 if fails else 0)
