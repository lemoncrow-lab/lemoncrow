import React, { type ReactNode, useEffect, useState } from "react";
import Link from "@docusaurus/Link";

import styles from "./styles.module.css";

const REPO = "lemoncrow-lab/lemoncrow";

function isGitHubRepo(value: unknown): value is { stargazers_count: number } {
  return (
    value !== null &&
    typeof value === "object" &&
    "stargazers_count" in value &&
    typeof (value as Record<string, unknown>).stargazers_count === "number"
  );
}

type Props = {
  className?: string;
};

// Same honest, live-fetched badge as landing/src/components/GitHubStars.tsx --
// no hardcoded count, renders nothing until a real number loads (and nothing
// at all if the API is unreachable).
export default function GithubStarsNavbarItem({ className }: Props): ReactNode {
  const [stars, setStars] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    fetch(`https://api.github.com/repos/${REPO}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d: unknown) => {
        if (alive && isGitHubRepo(d)) setStars(d.stargazers_count);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  if (stars === null) return null;

  const label = stars >= 1000 ? `${(stars / 1000).toFixed(1)}k` : `${stars}`;

  return (
    <Link
      href={`https://github.com/${REPO}`}
      className={[styles.badge, "navbar__item", className]
        .filter(Boolean)
        .join(" ")}
      aria-label={`${stars} GitHub stars`}
    >
      <svg
        viewBox="0 0 24 24"
        width="12"
        height="12"
        className={styles.star}
        aria-hidden="true"
      >
        <path d="M12 .587l3.668 7.568 8.332 1.151-6.064 5.828 1.48 8.279L12 19.771l-7.416 3.642 1.48-8.279L0 9.306l8.332-1.151z" />
      </svg>
      {label}
    </Link>
  );
}
