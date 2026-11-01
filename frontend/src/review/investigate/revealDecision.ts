// After the Reader switches to the Brief, bring one decision card into view and
// focus its first control. The card is the Brief's own (its write path is the
// Brief's); nothing here records anything.
// lc-debt: polls frames until the lazily loaded Brief renders the card, and finds nothing past its section's "Show all" bound; upgrade path: a Brief prop naming the decision to expand and focus.
const MAX_FRAMES = 60;

export function revealBriefDecision(decisionKey: string): void {
  let frames = 0;
  const attempt = () => {
    const card = [...document.querySelectorAll<HTMLElement>("[data-testid^='brief-decision-']")]
      .find((node) => node.dataset.testid === `brief-decision-${decisionKey}`);
    if (!card) {
      if (++frames < MAX_FRAMES) window.requestAnimationFrame(attempt);
      return;
    }
    card.scrollIntoView({ block: "center" });
    const control = card.querySelector<HTMLElement>("button:not([disabled]), [tabindex]:not([tabindex='-1'])");
    (control ?? card).focus({ preventScroll: true });
  };
  window.requestAnimationFrame(attempt);
}
