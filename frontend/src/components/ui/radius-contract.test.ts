import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const read = (file: string) => readFileSync(new URL(file, import.meta.url), 'utf8');

describe('LemonCrow radius system', () => {
  it('maps regular controls to 6px and panels to 8px through the shared preset', () => {
    expect(read('../../design/lc-preset.cjs')).toContain('borderRadius: { sm: "4px", DEFAULT: "6px", md: "6px", lg: "8px" }');
    expect(read('../../index.css')).toContain('--radius-control: theme("borderRadius.md");');
    expect(read('../../index.css')).toContain('--radius-panel: theme("borderRadius.lg");');
    expect(read('./button.tsx')).toContain('rounded-md');
    expect(read('./input.tsx')).toContain('rounded-md');
    expect(read('./card.tsx')).toContain('rounded-lg');
  });
  it('keeps review state badges on the 6px badge token', () => {
    expect(read('../../index.css')).toContain('--radius-badge: theme("borderRadius.DEFAULT");');
    expect(read('../../review/reviewUi.css')).toContain('border-radius: var(--radius-badge);');
  });
});
