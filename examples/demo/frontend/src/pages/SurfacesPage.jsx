import SurfaceCard from "../components/SurfaceCard.jsx";

const SURFACES = [
  { name: "Web", detail: "Live route preview" },
  { name: "API", detail: "Bruno request surfaces" },
  { name: "Runtime", detail: "Docker + command runners" },
  { name: "Components", detail: "Storybook story previews" },
  { name: "Data", detail: "Third-party CSV table plugin" },
  { name: "Media", detail: "GIF, video, audio, and PDF" },
];

export default function SurfacesPage() {
  return (
    <div className="page">
      <h2>Review surfaces</h2>
      <p className="lede">Each surface is discovered from the files a change touches.</p>
      <div className="grid">
        {SURFACES.map((surface) => (
          <SurfaceCard key={surface.name} {...surface} />
        ))}
      </div>
    </div>
  );
}
