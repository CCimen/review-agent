import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@fontsource/figtree/latin-400.css";
import "@fontsource/figtree/latin-500.css";
import "@fontsource/figtree/latin-600.css";
import "@fontsource/figtree/latin-700.css";
import { Preview } from "./Preview";

const root = document.getElementById("root");
if (!root) throw new Error("Preview root is missing");
createRoot(root).render(
  <StrictMode>
    <Preview />
  </StrictMode>,
);
