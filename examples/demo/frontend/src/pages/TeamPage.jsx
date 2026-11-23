const REVIEWERS = [
  { name: "Ada", role: "Frontend", open: 3 },
  { name: "Grace", role: "API", open: 1 },
  { name: "Linus", role: "Runtime", open: 0 },
  { name: "Margaret", role: "Release", open: 2 },
];

export default function TeamPage() {
  return (
    <div className="page">
      <h2>Team</h2>
      <p className="lede">Who reviews which surface.</p>
      <ul className="rows">
        {REVIEWERS.map((reviewer) => (
          <li key={reviewer.name}>
            <strong>{reviewer.name}</strong>
            <span>{reviewer.role}</span>
            <span>{reviewer.open} open</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
