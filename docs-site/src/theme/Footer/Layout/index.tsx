import React, { type ReactNode } from "react";
import clsx from "clsx";
import { ThemeClassNames } from "@docusaurus/theme-common";
import Logo from "@theme/Logo";
import type { Props } from "@theme/Footer/Layout";

import styles from "./styles.module.css";

// Logo lockup as col 1 + link columns alongside it, matching
// landing/src/components/Footer.tsx's grid instead of Infima's default
// centered-logo-below-links layout.
export default function FooterLayout({ links, copyright }: Props): ReactNode {
  return (
    <footer
      className={clsx(
        ThemeClassNames.layout.footer.container,
        "footer",
        styles.footer,
      )}
    >
      <div className={clsx("container container-fluid", styles.grid)}>
        <Logo className={styles.brand} />
        {links}
      </div>
      {copyright && (
        <div className={clsx("container container-fluid", styles.bottom)}>
          {copyright}
        </div>
      )}
    </footer>
  );
}
