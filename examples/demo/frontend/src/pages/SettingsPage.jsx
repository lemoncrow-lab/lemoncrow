const SETTINGS = [
  { name: "Preview sandbox", value: "bubblewrap, no network" },
  { name: "Snapshot pinning", value: "immutable revision" },
  { name: "Preview timeout", value: "120 s" },
  { name: "Artifact limit", value: "25 MB" },
];

export default function SettingsPage() {
  return (
    <div className="page">
      <h2>Settings</h2>
      <p className="lede">Review defaults for this workspace.</p>
      <ul className="rows">
        {SETTINGS.map((setting) => (
          <li key={setting.name}>
            <strong>{setting.name}</strong>
            <span>{setting.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
