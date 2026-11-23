/** @type {import("@storybook/react-vite").StorybookConfig} */
export default {
  stories: ["../src/**/*.stories.@(js|jsx)"],
  framework: { name: "@storybook/react-vite", options: {} },
  core: { disableTelemetry: true },
};
