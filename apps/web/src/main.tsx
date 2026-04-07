import React from "react";
import ReactDOM from "react-dom/client";
import "ol/ol.css";
import App from "./App";
import "./styles/tokens.css";
import "./styles/workbench.css";
import "./styles/map.css";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
