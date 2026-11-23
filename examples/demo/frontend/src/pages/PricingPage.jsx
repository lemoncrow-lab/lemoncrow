import { useEffect, useState } from "react";
import "./PricingPage.css";

// Prices and seats come from the API, which reads data/plans.csv; the page
// only decides how a card is drawn.
const CARD_STYLE = { Team: { featured: false } };

function usePlans() {
  const [state, setState] = useState({ status: "loading", plans: [] });
  useEffect(() => {
    let current = true;
    fetch("/api/plans")
      .then((response) => {
        if (!response.ok) throw new Error(`GET /api/plans answered ${response.status}`);
        return response.json();
      })
      .then((body) => current && setState({ status: "ready", plans: Array.isArray(body.plans) ? body.plans : [] }))
      .catch(() => current && setState({ status: "error", plans: [] }));
    return () => {
      current = false;
    };
  }, []);
  return state;
}

export default function PricingPage() {
  const { status, plans } = usePlans();
  return (
    <div className="page">
      <h2>Pricing</h2>
      <p className="lede">Plans for teams that review agent-written changes.</p>
      {status === "loading" ? (
        <p className="message" role="status">
          Loading plans…
        </p>
      ) : plans.length === 0 ? (
        <p className="message" role="status">
          Plans are unavailable right now.
        </p>
      ) : (
        <div className="plans">
          {plans.map((plan) => (
            <article key={plan.name} className={CARD_STYLE[plan.name]?.featured ? "plan plan--featured" : "plan"}>
              <strong>{plan.name}</strong>
              <span className="plan-price">
                ${plan.monthly}
                <small>/mo</small>
              </span>
              <span>{plan.seats} seats</span>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
