import {
  createContext,
  useContext,
  useReducer,
  useEffect,
  type ReactNode,
} from "react";

const GUEST_EMAIL = "guest@alphareport";
const EMAIL_KEY = "alphareport.email";

interface AuthState {
  email: string | null;
  isReady: boolean;
}

type AuthAction =
  | { type: "RESTORE"; email: string }
  | { type: "LOGOUT" };

function reducer(state: AuthState, action: AuthAction): AuthState {
  switch (action.type) {
    case "RESTORE":
      return { email: action.email, isReady: true };
    case "LOGOUT":
      return { email: null, isReady: true };
    default:
      return state;
  }
}

interface AuthContextValue {
  email: string | null;
  isReady: boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, {
    email: null,
    isReady: false,
  });

  // Auto-login as guest on mount (no email validation required)
  useEffect(() => {
    const saved = localStorage.getItem(EMAIL_KEY);
    dispatch({ type: "RESTORE", email: saved || GUEST_EMAIL });
  }, []);

  return (
    <AuthContext.Provider value={{ ...state }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return ctx;
}
