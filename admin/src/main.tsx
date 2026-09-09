import {
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import {
  createBrowserRouter,
  RouterProvider,
  ScrollRestoration,
} from "react-router-dom";
import { App } from "./App";
import { APIError } from "./api";
import "./theme.css";

const client = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      // A 404 means an optional endpoint is not configured on this deployment
      // (registration and OIDC both answer that way), not that the session went
      // stale, so it must not be read as an authentication signal.
      if (!(error instanceof APIError) || ![401, 403].includes(error.status))
        return;
      query.setState({ data: undefined });
      // Rechecking the account is only meaningful when we believed we had one.
      // On the sign-in screen "me" holds no data, and invalidating it there
      // re-rendered the page, which refetched these endpoints, which
      // invalidated it again: a request loop with no delay between passes.
      if (
        query.queryKey[0] !== "me" &&
        client.getQueryData(["me"]) !== undefined
      )
        void client.invalidateQueries({ queryKey: ["me"] });
    },
  }),
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
