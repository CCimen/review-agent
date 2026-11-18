import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../src/theme.css";
import { Preview } from "./Preview";

const root = document.getElementById("root");
if (!root) throw new Error("Preview root is missing");
createRoot(root).render(
  <StrictMode>
    <Preview />
  </StrictMode>,
);
