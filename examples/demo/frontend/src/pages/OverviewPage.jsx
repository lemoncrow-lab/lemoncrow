import { useState } from "react";
import content from "../content.json";

export default function OverviewPage() {
  const [message, setMessage] = useState("");
  return (
    <div className="hero">
      <p className="eyebrow">{content.eyebrow}</p>
      <h1>{content.title}</h1>
      <p className="lede">{content.body}</p>
      <button type="button" onClick={() => setMessage("This interaction is running in the real rendered preview.")}>
        {content.cta}
      </button>
      <p className="message" aria-live="polite">
        {message}
      </p>
    </div>
  );
}
