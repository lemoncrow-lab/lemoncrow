import "./SurfaceCard.css";

/** One product-surface card, shared by the Surfaces page and its Storybook stories. */
export default function SurfaceCard({ name, detail }) {
  return (
    <article className="surface-card">
      <strong>{name}</strong>
      <span>{detail}</span>
    </article>
  );
}
