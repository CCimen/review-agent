import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import {
  createBrowserRouter,
  RouterProvider,
  ScrollRestoration,
} from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { APIError } from "./api";
import "./fonts.css";
import "./styles.css";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchInterval: 10_000,
      refetchIntervalInBackground: false,
      retry: (count, error) =>
        count < 1 && !(error instanceof APIError && error.status < 500),
    },
  },
});

const router = createBrowserRouter([
  {
    path: "*",
    element: (
      <>
        <App />
        <ScrollRestoration
          getKey={(location) =>
            location.pathname === "/history"
              ? location.pathname + location.search
              : location.key
          }
        />
      </>
    ),
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
