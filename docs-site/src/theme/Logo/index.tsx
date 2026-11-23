import React, { type ReactNode } from "react";
import Link from "@docusaurus/Link";
import useBaseUrl from "@docusaurus/useBaseUrl";
import type { Props } from "@theme/Logo";

import styles from "./styles.module.css";

// Same lemon mark + wordmark lockup as landing/src/components/Nav.tsx:
// the site favicon (static/img/favicon.svg, identical to the landing's
// public/favicon.svg) next to the mono wordmark.
export default function Logo(props: Props): ReactNode {
  const { className, imageClassName, titleClassName, ...propsRest } = props;
  const logoLink = useBaseUrl("/");
  const mark = useBaseUrl("/img/favicon.svg");

  return (
    <Link
      to={logoLink}
      className={[styles.brand, className].filter(Boolean).join(" ")}
      {...propsRest}
    >
      <img
        src={mark}
        alt=""
        width={28}
        height={28}
        className={[styles.mark, imageClassName].filter(Boolean).join(" ")}
      />
      <span
        className={[styles.wordmark, titleClassName].filter(Boolean).join(" ")}
      >
        LEMONCROW
      </span>
    </Link>
  );
}
