"""What the gates under scripts/ share, written once and tested once.

Each gate is a top-level script, scripts/*.py, run on its own, and the
integrity floor counts every one of them as a gate it must see reject and
accept. A helper more than one gate needs lives in this package instead. The
floor does not count it as a gate, and the test job holds each file in it to
every line and branch.

A gate imports it by name. python3 puts a script's own directory first on
sys.path, so `import gatelib` finds this package however the gate is run.
"""
