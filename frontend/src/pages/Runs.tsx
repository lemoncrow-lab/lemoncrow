import { useSearchParams } from "react-router-dom";

import { PageFrame } from "../components/WorkbenchUI";
import { useProductCapabilities, useRuntimeCapabilities } from "../lib/RuntimeCapabilities";
import { PlansSection } from "./Learnings";
import Sessions from "./Sessions";
import Swarms from "./Swarms";

export default function Runs() {
  const [searchParams] = useSearchParams();
  const capabilities = useRuntimeCapabilities();
  const features = useProductCapabilities().features;
  const type = searchParams.get("type");
  if (type === "plans" && features["runtime.knowledge"]) {
    return (
      <PageFrame className="space-y-4">
        <PlansSection />
      </PageFrame>
    );
  }
  return capabilities.swarms && type === "swarms" ? <Swarms /> : <Sessions />;
}
