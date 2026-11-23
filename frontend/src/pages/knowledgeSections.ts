import { productAvailable, type LemonCrowCapabilities } from "../lib/RuntimeCapabilities";

/** The Knowledge tabs, by URL segment: /knowledge/<section>. `blocks` is Playbooks, `rubrics` is Checks. */
export type KnowledgeSection = "project" | "memory" | "blocks" | "rules" | "rubrics";

/**
 * A local Dashboard keeps no memory or playbook store. It answers Agent Memory
 * and Playbooks from the lessons its own sessions recorded, and Review rules
 * from its own reviews.
 */
export function sessionDerivedKnowledge(capabilities: LemonCrowCapabilities): boolean {
  return (
    capabilities.products.core.mode !== "hosted"
    && !capabilities.features["runtime.knowledge"]
    && capabilities.features["runtime.project_knowledge"]
  );
}

/** Whether this server can answer a Knowledge tab. */
export function knowledgeSectionServed(capabilities: LemonCrowCapabilities, section: string): boolean {
  const fullIndex = capabilities.features["runtime.knowledge"];
  switch (section) {
    case "project":
      return capabilities.features["runtime.project_knowledge"];
    case "memory":
    case "blocks":
      return fullIndex || sessionDerivedKnowledge(capabilities);
    case "rules":
      return productAvailable(capabilities, "review") && (fullIndex || sessionDerivedKnowledge(capabilities));
    case "rubrics":
      return fullIndex;
    default:
      return false;
  }
}
