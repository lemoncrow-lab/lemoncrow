import { useSearchParams } from "react-router-dom";

import { Select } from "../components/WorkbenchUI";
import Sessions from "./Sessions";
import Swarms from "./Swarms";

type RunType = "sessions" | "swarms";

export default function Runs() {
  const [searchParams, setSearchParams] = useSearchParams();
  const runType: RunType = searchParams.get("type") === "swarms" ? "swarms" : "sessions";

  const setRunType = (next: RunType) => {
    const params = new URLSearchParams(searchParams);
    if (next === "sessions") params.delete("type");
    else params.set("type", next);
    setSearchParams(params, { replace: true });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-end gap-3 px-5 pt-4 lg:px-6">
        <div className="flex items-center gap-2">
          <span className="text-[9px] font-semibold uppercase tracking-widest text-neutral-600">Type</span>
          <Select
            value={runType}
            onChange={(event) => setRunType(event.target.value as RunType)}
            uiSize="xs"
            aria-label="Run type"
          >
            <option value="sessions">Agent sessions</option>
            <option value="swarms">Swarm runs</option>
          </Select>
        </div>
      </div>

      {runType === "sessions" ? <Sessions /> : <Swarms />}
    </div>
  );
}
