/**
 * F6.8 — hook React para resolver una presigned URL S3 de un artifact.
 *
 * Reemplaza el patrón inseguro ``getArtifactUrl(uri)`` (JWT en query
 * string) por ``useArtifactS3Url(uri, cacheBust?)``. El hook:
 *
 *  1. Llama al backend con el ``Authorization`` header y obtiene una
 *     URL S3 firmada por HMAC de S3 (TTL 5 min, **sin JWT**).
 *  2. Expone ``{url, loading, error}``.
 *  3. Si ``cacheBust`` cambia, pide una URL nueva — útil cuando el
 *     active-learning retrain regenera el artifact y hay que refrescar
 *     el ``<img>``.
 *
 * Patrones típicos:
 *
 * ```tsx
 * // 1. Imagen simple
 * const { url } = useArtifactS3Url(uri);
 * return url ? <img src={url} /> : <Spinner />;
 *
 * // 2. Imagen con cache-bust
 * const { url } = useArtifactS3Url(roiArt?.uri, cacheBust);
 *
 * // 3. Varios artifacts a la vez
 * const { urls } = useArtifactsS3Urls({ thumb, pattern, attn, combined });
 * ```
 */
import { useEffect, useState } from 'react';
import { getArtifactS3Url } from '../api';

export interface ArtifactS3UrlState {
  url: string | null;
  loading: boolean;
  error: unknown;
}

export function useArtifactS3Url(
  uri: string | null | undefined,
  cacheBust?: unknown,
): ArtifactS3UrlState {
  const [state, setState] = useState<ArtifactS3UrlState>({
    url: null,
    loading: !!uri,
    error: null,
  });

  useEffect(() => {
    if (!uri) {
      setState({ url: null, loading: false, error: null });
      return;
    }
    let cancelled = false;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    getArtifactS3Url(uri)
      .then((resolved) => {
        if (!cancelled) setState({ url: resolved, loading: false, error: null });
      })
      .catch((err) => {
        if (!cancelled) setState({ url: null, loading: false, error: err });
      });
    return () => {
      cancelled = true;
    };
  }, [uri, cacheBust]);

  return state;
}

/**
 * Resuelve varias URLs S3 en paralelo. Las claves cuyo valor sea
 * ``null``/``undefined`` se preservan como ``null`` en la respuesta.
 */
export function useArtifactsS3Urls<K extends string>(
  uris: Record<K, string | null | undefined>,
  cacheBust?: unknown,
): {
  urls: Record<K, string | null>;
  loading: boolean;
  error: unknown;
} {
  const uriValues = Object.values(uris) as Array<string | null | undefined>;
  // Dependencia estable: una tira de las URIs en orden determinístico.
  const depKey = uriValues.join('|');

  const [state, setState] = useState<{
    urls: Record<K, string | null>;
    loading: boolean;
    error: unknown;
  }>(() => ({
    urls: Object.fromEntries(
      Object.keys(uris).map((k) => [k, null]),
    ) as Record<K, string | null>,
    loading: uriValues.some(Boolean),
    error: null,
  }));

  useEffect(() => {
    let cancelled = false;
    const entries = Object.entries(uris) as Array<[K, string | null | undefined]>;
    const activeEntries = entries.filter(([, v]) => !!v) as Array<[K, string]>;
    if (activeEntries.length === 0) {
      setState({
        urls: Object.fromEntries(
          entries.map(([k]) => [k, null]),
        ) as Record<K, string | null>,
        loading: false,
        error: null,
      });
      return;
    }
    setState((prev) => ({ ...prev, loading: true, error: null }));
    Promise.all(
      activeEntries.map(async ([k, v]) => [k, await getArtifactS3Url(v)] as const),
    )
      .then((pairs) => {
        if (cancelled) return;
        const next = Object.fromEntries(
          entries.map(([k]) => [k, null]),
        ) as Record<K, string | null>;
        for (const [k, url] of pairs) next[k] = url;
        setState({ urls: next, loading: false, error: null });
      })
      .catch((err) => {
        if (!cancelled) {
          setState((prev) => ({ ...prev, loading: false, error: err }));
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [depKey, cacheBust]);

  return state;
}
