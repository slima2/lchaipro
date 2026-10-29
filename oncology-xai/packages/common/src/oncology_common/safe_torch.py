"""Carga segura de modelos de PyTorch (SonarQube python:S6985).

torch.load con weights_only=False puede ejecutar código si alguien altera un archivo de modelo.
Con weights_only=True solo se aceptan tensores y tipos básicos; los checkpoints de lchai además
guardan números de numpy, que se permiten aquí de forma explícita (no son código ejecutable).
Verificado el 29-oct-2026: los 152 modelos de lchai-prod-models cargan así.
"""

from __future__ import annotations


def allow_numpy_globals() -> None:
    """Permite en torch.load(weights_only=True) los tipos de numpy que traen los checkpoints."""
    import numpy as np
    import torch

    try:
        from numpy._core.multiarray import scalar  # numpy 2.x
    except ImportError:  # numpy 1.x
        from numpy.core.multiarray import scalar

    dtypes = [type(np.dtype(t)) for t in ("float64", "float32", "float16", "int64", "int32", "bool")]
    torch.serialization.add_safe_globals([scalar, np.dtype, *dtypes])
