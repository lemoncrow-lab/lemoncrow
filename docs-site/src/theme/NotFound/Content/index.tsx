import React, { type ReactNode } from "react";
import clsx from "clsx";
import Link from "@docusaurus/Link";
import Heading from "@theme/Heading";
import type { Props } from "@theme/NotFound/Content";

import styles from "./styles.module.css";

const PAGES = [
  { to: "/", label: "Docs home" },
  { to: "/setup/installation", label: "Installation" },
  { to: "/reference/cli", label: "CLI reference" },
  { to: "/setup/troubleshooting", label: "Troubleshooting" },
];

export default function NotFoundContent({ className }: Props): ReactNode {
  return (
    <main className={clsx("container margin-vert--xl", styles.main, className)}>
      <p className={styles.eyebrow}>404</p>
      <Heading as="h1">This page does not exist</Heading>
      <p>The link may be old, or the page may have moved. Try one of these:</p>
      <ul className={styles.links}>
        {PAGES.map((page) => (
          <li key={page.to}>
            <Link to={page.to}>{page.label}</Link>
          </li>
        ))}
      </ul>
      <p>
        Or go back to <Link href="https://lemoncrow.com">lemoncrow.com</Link>.
      </p>
    </main>
  );
}
