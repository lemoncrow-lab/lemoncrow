import React, { type ReactNode } from "react";
import Link from "@docusaurus/Link";
import useBaseUrl from "@docusaurus/useBaseUrl";
import type { Props } from "@theme/Logo";

import styles from "./styles.module.css";

// Same chip + wordmark lockup as landing/src/components/Nav.tsx and
// Footer.tsx -- kept as inline markup (not an image) so both render
// identically without shipping a logo asset.
export default function Logo(props: Props): ReactNode {
  const { className, imageClassName, titleClassName, ...propsRest } = props;
  const logoLink = useBaseUrl("/");

  return (
    <Link
      to={logoLink}
      className={[styles.brand, className].filter(Boolean).join(" ")}
      {...propsRest}
    >
      <span
        className={[styles.chip, imageClassName].filter(Boolean).join(" ")}
        aria-hidden="true"
      >
        {"❯"}
      </span>
      <span
        className={[styles.wordmark, titleClassName].filter(Boolean).join(" ")}
      >
        ATELIER
      </span>
    </Link>
  );
}
