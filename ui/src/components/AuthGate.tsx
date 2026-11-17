import { useEffect, type ReactNode } from "react";
import { SignIn, useAuth } from "@clerk/react";
import { useQueryClient } from "@tanstack/react-query";
import { setTokenGetter } from "../lib/api";
import { Loading } from "./ui";

export function AuthGate({ children }: { children: ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const qc = useQueryClient();
  setTokenGetter(() => getToken());
  useEffect(() => {
    if (isLoaded && !isSignedIn) qc.clear();
  }, [isLoaded, isSignedIn, qc]);
  if (!isLoaded) return <Loading />;
  if (!isSignedIn) return <div className="login"><SignIn routing="hash" /></div>;
  return <>{children}</>;
}
