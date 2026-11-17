import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ClerkProvider } from "@clerk/react";
import App from "./App";
import { AuthGate } from "./components/AuthGate";
import "./styles.css";

const client = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } });
const clerkKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
      <ClerkProvider
      publishableKey={clerkKey}
      afterSignOutUrl="/"
      appearance={{
        variables: {
          colorBackground: "#161b22",
          colorText: "#e6edf3",
          colorTextSecondary: "#8b949e",
          colorInputBackground: "#0d1117",
          colorInputText: "#e6edf3",
          colorPrimary: "#2f81f7",
          colorNeutral: "#e6edf3",
          borderRadius: "10px",
        },
      }}
    >
      <QueryClientProvider client={client}>
        <BrowserRouter>
          <AuthGate><App /></AuthGate>
        </BrowserRouter>
      </QueryClientProvider>
    </ClerkProvider>
  </StrictMode>,
);