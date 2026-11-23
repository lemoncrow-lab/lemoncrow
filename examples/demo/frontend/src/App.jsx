import { NavLink, Route, Routes } from "react-router-dom";
import markUrl from "../public/cloud-mark.svg";
import OverviewPage from "./pages/OverviewPage.jsx";
import PricingPage from "./pages/PricingPage.jsx";
import SettingsPage from "./pages/SettingsPage.jsx";
import SurfacesPage from "./pages/SurfacesPage.jsx";
import TeamPage from "./pages/TeamPage.jsx";

const LINKS = [
  ["/", "Overview"],
  ["/pricing", "Pricing"],
  ["/surfaces", "Surfaces"],
  ["/team", "Team"],
  ["/settings", "Settings"],
];

// Each page is its own module, so LemonCrow Review maps a changed file to
// exactly the routes that render it instead of to every page of the app.
export default function App() {
  return (
    <section className="shell">
      <header className="nav">
        <img className="mark" src={markUrl} alt="" />
        <span>LemonCrow Cloud</span>
        <nav>
          {LINKS.map(([to, label]) => (
            <NavLink key={to} to={to} end>
              {label}
            </NavLink>
          ))}
        </nav>
      </header>
      <Routes>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/pricing" element={<PricingPage />} />
        <Route path="/surfaces" element={<SurfacesPage />} />
        <Route path="/team" element={<TeamPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Routes>
    </section>
  );
}
