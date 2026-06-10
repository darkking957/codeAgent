import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/scrollbar.css";
import "./styles/ui.css";
import "./styles/layout.css";
import "./styles/sidebar.css";
import "./styles/home.css";
import "./styles/composer.css";
import "./styles/conversation.css";
import "./styles/files.css";
import "./styles/approval.css";
import "./styles/auth.css";
import "./styles/markdown.css";
import "./styles/codehilite.css";
import "./styles/responsive.css";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
