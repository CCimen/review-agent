import SwaggerUI from "swagger-ui";
import "swagger-ui/dist/swagger-ui.css";
import "./fonts.css";
import "./apiDocs.css";

SwaggerUI({
  dom_id: "#swagger-ui",
  url: "/api/openapi.json",
  validatorUrl: "none",
  withCredentials: true,
  persistAuthorization: false,
  queryConfigEnabled: false,
  deepLinking: true,
  filter: true,
  docExpansion: "none",
  tryItOutEnabled: false,
});
