/*
 * orf_core.c — C accelerator for _find_longest_orf.
 *
 * Scans DNA sequences for the longest methionine-initiated ORF across
 * all 6 reading frames (3 forward + 3 reverse complement).
 *
 * The reverse-complement scan reads the forward buffer backwards,
 * checking for complement-reversed codons (CAT→ATG, TTA→TAA, etc.),
 * avoiding a separate RC buffer and halving memory traffic.
 *
 * scan_batch() processes many sequences in parallel using pthreads,
 * with the GIL released during the compute phase.
 *
 * Build:  python3 devtools/build_orf_core.py
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>

/* ── Lookup tables ─────────────────────────────────────────────────── */

static unsigned char UPPER[256];

static void
init_tables(void)
{
    for (int i = 0; i < 256; i++)
        UPPER[i] = (unsigned char)i;
    UPPER['a'] = 'A'; UPPER['c'] = 'C'; UPPER['g'] = 'G';
    UPPER['t'] = 'T'; UPPER['n'] = 'N';
}

/* ── Codon checks ─────────────────────────────────────────────────── */

#define IS_ATG(c0,c1,c2)  ((c0)=='A' && (c1)=='T' && (c2)=='G')
#define IS_STOP(c0,c1,c2) ((c0)=='T' && \
    (((c1)=='A' && ((c2)=='A' || (c2)=='G')) || ((c1)=='G' && (c2)=='A')))

#define IS_RC_ATG(c0,c1,c2)  ((c0)=='C' && (c1)=='A' && (c2)=='T')
#define IS_RC_STOP(c0,c1,c2) ((c2)=='A' && \
    (((c0)=='T' && ((c1)=='T' || (c1)=='C')) || ((c0)=='C' && (c1)=='T')))

/* ── Core scanner (pure C, no Python API) ─────────────────────────── */

typedef struct {
    int        frame;
    Py_ssize_t atg;
    Py_ssize_t stop;
    Py_ssize_t length;
    int        strand;    /* 0 = fwd, 1 = rc */
} ORFResult;

static void
scan_both(const unsigned char *buf, Py_ssize_t len, ORFResult *best)
{
    best->frame  = -1;
    best->atg    = -1;
    best->stop   = -1;
    best->length = 0;
    best->strand = 0;

    Py_ssize_t end = len - 2;

    /* Forward strand */
    for (int fi = 0; fi < 3; fi++) {
        Py_ssize_t first_atg = -1;
        for (Py_ssize_t pos = fi; pos < end; pos += 3) {
            unsigned char c0 = buf[pos];
            unsigned char c1 = buf[pos + 1];
            unsigned char c2 = buf[pos + 2];
            if (IS_ATG(c0, c1, c2)) {
                if (first_atg == -1) first_atg = pos;
            } else if (IS_STOP(c0, c1, c2)) {
                if (first_atg != -1) {
                    Py_ssize_t l = (pos - first_atg) / 3;
                    if (l > best->length) {
                        best->length = l;
                        best->frame  = fi;
                        best->atg    = first_atg;
                        best->stop   = pos;
                        best->strand = 0;
                    }
                }
                first_atg = -1;
            }
        }
    }

    /* Reverse complement via reverse read */
    for (int fi = 0; fi < 3; fi++) {
        Py_ssize_t first_atg_j = -1;
        for (Py_ssize_t p = len - 3 - fi; p >= 0; p -= 3) {
            unsigned char c0 = buf[p];
            unsigned char c1 = buf[p + 1];
            unsigned char c2 = buf[p + 2];
            Py_ssize_t rc_j = len - 3 - p;
            if (IS_RC_ATG(c0, c1, c2)) {
                if (first_atg_j == -1) first_atg_j = rc_j;
            } else if (IS_RC_STOP(c0, c1, c2)) {
                if (first_atg_j != -1) {
                    Py_ssize_t l = (rc_j - first_atg_j) / 3;
                    if (l > best->length) {
                        best->length = l;
                        best->frame  = fi;
                        best->atg    = first_atg_j;
                        best->stop   = rc_j;
                        best->strand = 1;
                    }
                }
                first_atg_j = -1;
            }
        }
    }
}

/*
 * Process one sequence: uppercase into a temp buffer, scan, store result.
 * Pure C — no Python API, safe to call without the GIL.
 */
static void
scan_one(const unsigned char *raw, Py_ssize_t len, ORFResult *result)
{
    /* Uppercase into a local buffer. */
    unsigned char stack_buf[16384];
    unsigned char *buf;
    int heap = (len > (Py_ssize_t)sizeof(stack_buf));

    if (heap) {
        buf = (unsigned char *)malloc(len);
        if (!buf) { result->length = 0; result->frame = -1; return; }
    } else {
        buf = stack_buf;
    }

    for (Py_ssize_t i = 0; i < len; i++)
        buf[i] = UPPER[raw[i]];

    scan_both(buf, len, result);

    if (heap) free(buf);
}

/* ── Batch scanner with pthreads ──────────────────────────────────── */

typedef struct {
    const unsigned char *raw;
    Py_ssize_t           len;
    ORFResult            result;
} ScanTask;

typedef struct {
    ScanTask *tasks;
    Py_ssize_t start;
    Py_ssize_t end;
} WorkerArg;

static void *
worker_thread(void *arg)
{
    WorkerArg *w = (WorkerArg *)arg;
    for (Py_ssize_t i = w->start; i < w->end; i++) {
        scan_one(w->tasks[i].raw, w->tasks[i].len, &w->tasks[i].result);
    }
    return NULL;
}

/* ── Python-facing functions ──────────────────────────────────────── */

/*
 * scan_both_strands(nucleotide: str|bytes)
 *   -> (strand_idx, frame, atg_pos, stop_pos, aa_length)
 */
static PyObject *
py_scan_both_strands(PyObject *self, PyObject *args)
{
    PyObject *obj;
    const unsigned char *raw;
    Py_ssize_t len;

    if (!PyArg_ParseTuple(args, "O", &obj))
        return NULL;

    if (PyUnicode_Check(obj)) {
        const char *utf8 = PyUnicode_AsUTF8AndSize(obj, &len);
        if (!utf8) return NULL;
        raw = (const unsigned char *)utf8;
    } else if (PyBytes_Check(obj)) {
        raw = (const unsigned char *)PyBytes_AS_STRING(obj);
        len = PyBytes_GET_SIZE(obj);
    } else {
        PyErr_SetString(PyExc_TypeError, "expected str or bytes");
        return NULL;
    }

    ORFResult best;
    scan_one(raw, len, &best);

    return Py_BuildValue("(innnn)",
                         best.strand,
                         best.frame, best.atg, best.stop, best.length);
}

/*
 * scan_batch(sequences: list[str], nthreads: int)
 *   -> list[(strand_idx, frame, atg_pos, stop_pos, aa_length)]
 *
 * Scans all sequences in parallel using pthreads.
 * The GIL is released during the compute phase.
 * Falls back to single-threaded if nthreads <= 1.
 */
static PyObject *
py_scan_batch(PyObject *self, PyObject *args)
{
    PyObject *seq_list;
    int nthreads;
    if (!PyArg_ParseTuple(args, "Oi", &seq_list, &nthreads))
        return NULL;

    if (!PyList_Check(seq_list)) {
        PyErr_SetString(PyExc_TypeError, "first argument must be a list");
        return NULL;
    }

    Py_ssize_t n = PyList_GET_SIZE(seq_list);
    if (n == 0)
        return PyList_New(0);

    /* Phase 1 (GIL held): extract raw pointers from Python objects. */
    ScanTask *tasks = (ScanTask *)calloc(n, sizeof(ScanTask));
    if (!tasks) return PyErr_NoMemory();

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *obj = PyList_GET_ITEM(seq_list, i);
        if (PyUnicode_Check(obj)) {
            const char *utf8 = PyUnicode_AsUTF8AndSize(obj, &tasks[i].len);
            if (!utf8) { free(tasks); return NULL; }
            tasks[i].raw = (const unsigned char *)utf8;
        } else if (PyBytes_Check(obj)) {
            tasks[i].raw = (const unsigned char *)PyBytes_AS_STRING(obj);
            tasks[i].len = PyBytes_GET_SIZE(obj);
        } else {
            free(tasks);
            PyErr_SetString(PyExc_TypeError,
                            "list elements must be str or bytes");
            return NULL;
        }
    }

    /* Phase 2 (GIL released): scan in parallel. */
    if (nthreads < 1) nthreads = 1;
    if (nthreads > (int)n) nthreads = (int)n;

    Py_BEGIN_ALLOW_THREADS

    if (nthreads == 1) {
        /* Single-threaded fast path — no pthread overhead. */
        for (Py_ssize_t i = 0; i < n; i++)
            scan_one(tasks[i].raw, tasks[i].len, &tasks[i].result);
    } else {
        pthread_t *threads = (pthread_t *)malloc(nthreads * sizeof(pthread_t));
        WorkerArg *wargs   = (WorkerArg *)malloc(nthreads * sizeof(WorkerArg));

        Py_ssize_t chunk = n / nthreads;
        Py_ssize_t rem   = n % nthreads;
        Py_ssize_t offset = 0;

        for (int t = 0; t < nthreads; t++) {
            wargs[t].tasks = tasks;
            wargs[t].start = offset;
            wargs[t].end   = offset + chunk + (t < rem ? 1 : 0);
            offset = wargs[t].end;
            pthread_create(&threads[t], NULL, worker_thread, &wargs[t]);
        }

        for (int t = 0; t < nthreads; t++)
            pthread_join(threads[t], NULL);

        free(threads);
        free(wargs);
    }

    Py_END_ALLOW_THREADS

    /* Phase 3 (GIL held): build result list. */
    PyObject *result_list = PyList_New(n);
    if (!result_list) { free(tasks); return NULL; }

    for (Py_ssize_t i = 0; i < n; i++) {
        ORFResult *r = &tasks[i].result;
        PyObject *tup = Py_BuildValue("(innnn)",
                                      r->strand, r->frame,
                                      r->atg, r->stop, r->length);
        if (!tup) {
            Py_DECREF(result_list);
            free(tasks);
            return NULL;
        }
        PyList_SET_ITEM(result_list, i, tup);
    }

    free(tasks);
    return result_list;
}

/*
 * scan_strand(strand: bytes) -> (frame, atg_pos, stop_pos, aa_length)
 */
static PyObject *
py_scan_strand(PyObject *self, PyObject *args)
{
    Py_buffer pybuf;
    if (!PyArg_ParseTuple(args, "y*", &pybuf))
        return NULL;

    ORFResult best;
    best.frame = -1; best.atg = -1; best.stop = -1;
    best.length = 0; best.strand = 0;

    Py_ssize_t end = pybuf.len - 2;
    const unsigned char *raw = (const unsigned char *)pybuf.buf;
    for (int fi = 0; fi < 3; fi++) {
        Py_ssize_t first_atg = -1;
        for (Py_ssize_t pos = fi; pos < end; pos += 3) {
            unsigned char c0 = raw[pos], c1 = raw[pos+1], c2 = raw[pos+2];
            if (IS_ATG(c0,c1,c2)) {
                if (first_atg == -1) first_atg = pos;
            } else if (IS_STOP(c0,c1,c2)) {
                if (first_atg != -1) {
                    Py_ssize_t l = (pos - first_atg) / 3;
                    if (l > best.length) {
                        best.length = l; best.frame = fi;
                        best.atg = first_atg; best.stop = pos;
                    }
                }
                first_atg = -1;
            }
        }
    }

    PyBuffer_Release(&pybuf);
    return Py_BuildValue("(nnnn)",
                         (Py_ssize_t)best.frame, best.atg,
                         best.stop, best.length);
}

/* ── Module definition ────────────────────────────────────────────── */

static PyMethodDef orf_core_methods[] = {
    {"scan_strand", py_scan_strand, METH_VARARGS,
     "scan_strand(strand: bytes) -> (frame, atg_pos, stop_pos, aa_length)\n"
     "Scan one uppercase strand for the longest M-initiated ORF."},
    {"scan_both_strands", py_scan_both_strands, METH_VARARGS,
     "scan_both_strands(nucleotide: str|bytes) -> (strand, frame, atg, stop, length)\n"
     "Uppercase, compute RC, scan both strands. strand: 0=fwd, 1=rc."},
    {"scan_batch", py_scan_batch, METH_VARARGS,
     "scan_batch(sequences: list[str], nthreads: int) -> list[tuple]\n"
     "Scan all sequences in parallel using pthreads.\n"
     "Returns list of (strand, frame, atg, stop, length) tuples."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef orf_core_module = {
    PyModuleDef_HEAD_INIT,
    "orf_core",
    "C accelerator for ORF scanning.",
    -1,
    orf_core_methods
};

PyMODINIT_FUNC
PyInit_orf_core(void)
{
    init_tables();
    return PyModule_Create(&orf_core_module);
}
