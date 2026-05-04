"""
Correlations and Distances to Landmarks (Mexico City).

This module computes the distance from Airbnb listings/hosts to a set of city
landmarks (Haversine distance, in kilometers) and then computes correlations
between those distances and host-level variables.

Inputs
------
hosts.csv : CSV
    Must include at least ``latitude`` and ``longitude``.
places.csv : CSV
    Must include ``place_name``, ``latitude``, and ``longitude``.

Outputs
-------
hosts_with_distances.csv : CSV
    The input host dataset augmented with distance columns named ``d_t_<slug>``.
<result>_places_mapping.csv : CSV
    Mapping between the original ``place_name`` and the generated ``place_slug``.
Correlation matrix figures : PDF and PNG
    Heatmap exports for exploratory analysis.
Correlation summary tables : CSV
    Per-distance correlation, sample size, p-value, and BH-FDR q-value (if SciPy
    is available).

Notes
-----
Distance columns are generated dynamically from the places file using a stable,
ASCII-safe slug derived from ``place_name``. This makes the pipeline robust to
changes in the number of landmarks and to punctuation/accents in place names.

Credits
-------
Developed by:
    Dr. Gerardo Tinoco Guerrero (gerardo.tinoco@umich.mx)
    Dr. José Alberto Guzmán Torres (jose.alberto.guzman@umich.mx)
    Dr. Narciso Salvador Tinoco Guerrero (narciso.tinoco@umich.mx)
    Universidad Michoacana de San Nicolás de Hidalgo

Funding:
    Secretaria de Ciencias, Humanidades, Tecnología e Innovación, SeCiHTI, México.
    (Secretariat of Science, Humanities, Technology and Innovation, SeCiHTI, Mexico.)
    Aula CIMNE-Morelia. México

Created: October 2023
Last modified: May 2026
"""

# Imports
import os
import re
import tarfile
import unicodedata

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

def _read_csv_with_fallback(path):
    """
    Read a CSV file using a simple encoding fallback strategy.

    Parameters
    ----------
    path : str or path-like
        Path to the CSV file to be loaded.

    Returns
    -------
    pandas.DataFrame
        DataFrame containing the data read from disk.

    Notes
    -----
    The function first tries UTF-8 and, if that fails due to a decoding error,
    falls back to ISO-8859-1 to preserve compatibility with legacy datasets.
    """
    try:
        return pd.read_csv(path, encoding="utf-8")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="ISO-8859-1")

def _ensure_parent_dir(file_path):
    """
    Create the parent directory for a target file if it does not already exist.

    Parameters
    ----------
    file_path : str or path-like
        Destination file path whose parent directory should be created.
    """
    parent = os.path.dirname(str(file_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

def _slugify(value):
    """
    Normalize a text value into an ASCII-safe slug suitable for column names.

    Parameters
    ----------
    value : Any
        Original value that will be converted into a normalized text label.

    Returns
    -------
    str
        Lowercase slug using only letters, numbers, and underscores.
    """
    s = str(value).strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s

def _places_mapping_path_from_result(result_path):
    """
    Build the path for the places mapping CSV associated with a result file.

    Parameters
    ----------
    result_path : str or path-like
        Path to the main distances output file.

    Returns
    -------
    str
        Path where the place name/slug mapping will be stored.
    """
    base, _ext = os.path.splitext(str(result_path))
    return base + "_places_mapping.csv"

def _corr_table_path_from_matrix_path(matrix_path):
    """
    Build the output path for the correlation summary table.

    Parameters
    ----------
    matrix_path : str or path-like
        Base path used for the correlation matrix exports.

    Returns
    -------
    str
        Path where the correlation table CSV will be stored.
    """
    return str(matrix_path) + "_corr_table.csv"

def _bh_fdr(p_values):
    """
    Apply the Benjamini-Hochberg false discovery rate correction.

    Parameters
    ----------
    p_values : array-like
        Sequence of raw p-values.

    Returns
    -------
    numpy.ndarray
        Adjusted q-values in the same order as the input p-values.
    """
    p    = np.asarray(p_values, dtype=float)
    out  = np.full_like(p, np.nan, dtype=float)

    mask = np.isfinite(p)
    if not np.any(mask):
        return out

    p0     = p[mask]
    n      = p0.size
    order  = np.argsort(p0)
    ranked = p0[order]

    q = ranked * n / (np.arange(1, n + 1))
    # Ensure monotonicity (BH step-up)
    q = np.minimum.accumulate(q[::-1])[::-1]
    q = np.clip(q, 0.0, 1.0)

    out0        = np.empty_like(q)
    out0[order] = q
    out[mask]   = out0
    return out

def _try_pearson_pvalues_from_r(r_values, n):
    """
    Compute two-sided Pearson p-values from correlation coefficients.

    Parameters
    ----------
    r_values : array-like
        Pearson correlation coefficients.
    n : int
        Number of observations used to compute the correlations.

    Returns
    -------
    tuple
        Pair ``(p_values, note)`` where ``p_values`` is a NumPy array when
        SciPy is available, or ``None`` otherwise. ``note`` contains an
        explanatory message when p-values could not be computed.

    Notes
    -----
    This implementation derives p-values from the t distribution instead of
    calling ``scipy.stats.pearsonr`` repeatedly, which is faster and avoids
    numerical warnings observed on large datasets.
    """
    try:
        from scipy.stats import t  # type: ignore
    except Exception:
        return None, "scipy not available; p-values were not computed."

    df = int(n) - 2
    if df <= 0:
        return np.full_like(np.asarray(r_values, dtype=float), np.nan, dtype=float), None

    r      = np.asarray(r_values, dtype=float)
    # Avoid division by zero when r is exactly +/-1 due to numeric rounding.
    eps    = 1e-15
    r      = np.clip(r, -1.0 + eps, 1.0 - eps)
    t_stat = r * np.sqrt(df / (1.0 - r**2))
    p      = 2.0 * t.sf(np.abs(t_stat), df)
    return p, None

def _haversine_km_vectorized(lat1_rad, lon1_rad, lat2_rad, lon2_rad):
    """
    Compute Haversine distances in kilometers using vectorized NumPy operations.

    Parameters
    ----------
    lat1_rad : numpy.ndarray
        Latitudes of the first set of points, in radians.
    lon1_rad : numpy.ndarray
        Longitudes of the first set of points, in radians.
    lat2_rad : float
        Latitude of the second point, in radians.
    lon2_rad : float
        Longitude of the second point, in radians.

    Returns
    -------
    numpy.ndarray
        Great-circle distances in kilometers.
    """
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a    = np.sin(dlat / 2.0) ** 2 + np.cos(lat1_rad) * np.cos(lat2_rad) * (np.sin(dlon / 2.0) ** 2)
    c    = 2.0 * np.arcsin(np.sqrt(a))
    return 6371.0088 * c

def distances(hosts="Information/hosts.csv", places="Information/places.csv", result="Results/hosts_with_distances.csv"):
    """
    Compute distances from Airbnb hosts to all places listed in a landmarks file.

    Parameters
    ----------
    hosts : str, optional
        Path to the CSV file containing Airbnb host/listing information.
    places : str, optional
        Path to the CSV file containing landmark names and coordinates.
    result : str, optional
        Path where the filtered dataset with distance columns will be saved.

    Returns
    -------
    None
        The function writes the processed dataset to disk and also generates a
        compressed ``.tar.gz`` version of the result file.

    Notes
    -----
    Distance columns are generated dynamically from the places file using the
    prefix ``d_t_`` plus a normalized slug derived from ``place_name``.
    """

    # Load data from CSV files.
    try:
        hosts_df  = _read_csv_with_fallback(hosts)
        places_df = _read_csv_with_fallback(places)
    except Exception as e:
        print(f"An error occurred when reading the CSV files: {e}")
        return

    # Validate the minimum schema required to compute geographic distances.
    required_hosts_cols  = {"latitude", "longitude"}
    required_places_cols = {"place_name", "latitude", "longitude"}
    if not required_hosts_cols.issubset(set(hosts_df.columns)):
        missing = sorted(required_hosts_cols - set(hosts_df.columns))
        raise ValueError(f"hosts.csv is missing required columns: {missing}")
    if not required_places_cols.issubset(set(places_df.columns)):
        missing = sorted(required_places_cols - set(places_df.columns))
        raise ValueError(f"places.csv is missing required columns: {missing}")

    # Coordinates must be numeric before applying the Haversine formula.
    hosts_df               = hosts_df.copy()
    hosts_df["latitude"]   = pd.to_numeric(hosts_df["latitude"], errors="coerce")
    hosts_df["longitude"]  = pd.to_numeric(hosts_df["longitude"], errors="coerce")
    hosts_df               = hosts_df.dropna(subset=["latitude", "longitude"])

    places_df              = places_df.copy()
    places_df["latitude"]  = pd.to_numeric(places_df["latitude"], errors="coerce")
    places_df["longitude"] = pd.to_numeric(places_df["longitude"], errors="coerce")
    places_df              = places_df.dropna(subset=["place_name", "latitude", "longitude"])

    # Normalize place names so output columns remain stable across platforms
    # and do not depend on accents, spaces, or punctuation.
    places_df["place_slug"] = places_df["place_name"].map(_slugify)
    if places_df["place_slug"].isna().any() or (places_df["place_slug"].astype(str).str.len() == 0).any():
        raise ValueError("Some places have empty slugs after normalization. Check 'place_name' values.")
    
    dup_slugs = places_df["place_slug"][places_df["place_slug"].duplicated()].unique().tolist()
    if dup_slugs:
        raise ValueError(f"Duplicate place slugs detected: {dup_slugs}. Please adjust place names.")

    # Convert host coordinates once so each landmark can reuse the same arrays.
    host_lat = np.radians(hosts_df["latitude"].to_numpy(dtype=float))
    host_lon = np.radians(hosts_df["longitude"].to_numpy(dtype=float))

    distance_cols_in_order = []
    for _, place in places_df.iterrows():
        place_slug    = str(place["place_slug"]).strip()
        col           = f"d_t_{place_slug}"
        distance_cols_in_order.append(col)

        place_lat     = float(place["latitude"])
        place_lon     = float(place["longitude"])
        # Compute all host-to-place distances at once for the current landmark.
        distances_km  = _haversine_km_vectorized(
            host_lat,
            host_lon,
            np.radians(place_lat),
            np.radians(place_lon),
        )
        hosts_df[col] = distances_km

    # Store the original place names together with their normalized slugs.
    mapping_path = _places_mapping_path_from_result(result)
    _ensure_parent_dir(mapping_path)
    places_df[["place_name", "place_slug", "latitude", "longitude"]].to_csv(mapping_path, index=False)
    print("Places mapping saved to", mapping_path)

    # Keep the host metadata needed for the study plus all generated distances.
    base_cols     = [
        "id",
        "host_id",
        "host_url",
        "host_name",
        "host_is_superhost",
        "host_listings_count",
        "host_total_listings_count",
    ]
    cols_present  = [c for c in base_cols if c in hosts_df.columns] + distance_cols_in_order
    filtered_data = hosts_df[cols_present].copy()

    if "host_is_superhost" in filtered_data.columns:
        col = filtered_data["host_is_superhost"]
        if pd.api.types.is_bool_dtype(col):
            filtered_data["host_is_superhost"] = col.astype(int)
        elif pd.api.types.is_numeric_dtype(col):
            filtered_data["host_is_superhost"] = pd.to_numeric(col, errors="coerce")
        else:
            # Convert common boolean encodings such as "t"/"f" into numeric form.
            s      = col.astype("string").str.strip().str.lower()
            mapped = pd.Series(np.nan, index=col.index, dtype="float64")
            mapped[s == "t"] = 1.0
            mapped[s == "f"] = 0.0
            # If values are already numeric-ish ("0"/"1"), keep them.
            mapped = mapped.where(mapped.notna(), pd.to_numeric(col, errors="coerce"))
            filtered_data["host_is_superhost"] = mapped

    # Preserve the original behavior by removing incomplete rows in the final output.
    filtered_data  = filtered_data.dropna()

    # Saving the updated DataFrame with the new distance information to a new CSV file.
    _ensure_parent_dir(result)
    filtered_data.to_csv(result, index=False)

    # Console message indicating successful completion of the script.
    print("The distances have been calculated and saved to", result)
    make_tarfile(result + ".tar.gz", result)
    print("The file was compressed and saved to", result + ".tar.gz")

def Distances(hosts="Information/hosts.csv", places="Information/places.csv", result="Results/hosts_with_distances.csv"):
    """
    Backwards-compatible wrapper for :func:`distances`.

    Notes
    -----
    Kept to avoid breaking existing notebooks/scripts that call ``Distances(...)``.
    Prefer using :func:`distances` in new code.
    """
    return distances(hosts=hosts, places=places, result=result)

def make_tarfile(output_filename, source_file):
    """
    Compress a file into ``.tar.gz`` format.

    Parameters
    ----------
    output_filename : str
        Name of the compressed output file.
    source_file : str
        Path to the file that will be added to the archive.
    """
    with tarfile.open(output_filename, "w:gz") as tar:
        tar.add(source_file)

def correlate_against_variable(filename, variable, matrix_path):
    """
    Compute correlations between an existing numeric variable and all distance columns.

    Parameters
    ----------
    filename : str
        Path to the CSV dataset containing host information and distance columns.
    variable : str
        Name of the existing variable to correlate against the distances.
    matrix_path : str
        Base path used to export the correlation matrix and summary table.
    """
    # Load the dataset and validate that the requested variable exists.
    data = _read_csv_with_fallback(filename)

    if variable not in data.columns:
        raise ValueError(f"Variable '{variable}' not found in data columns.")

    distance_cols = [c for c in data.columns if str(c).startswith("d_t_")]
    if not distance_cols:
        raise ValueError("No distance columns found (expected columns starting with 'd_t_').")

    # Select only the target variable and the generated distance columns.
    data_interest = data[[variable] + distance_cols].copy()
    for c in data_interest.columns:
        data_interest[c] = pd.to_numeric(data_interest[c], errors="coerce")
    # User-selected behavior: drop rows where variable or any distance is missing.
    data_interest = data_interest.dropna(subset=[variable] + distance_cols)

    # Compute the full Pearson correlation matrix for visualization purposes.
    correlations  = data_interest.corr(method="pearson")

    # Export the heatmap in vector and raster formats.
    _ensure_parent_dir(matrix_path)
    fig = plt.figure(figsize=(30, 24))
    sns.heatmap(correlations, annot=True, fmt=".2f", cmap="YlGnBu", vmin=-1, vmax=1, cbar=True)
    plt.title("Correlation Matrix.")
    plt.xticks(rotation=90)
    plt.yticks(rotation=0)
    fig.tight_layout()
    fig.savefig(matrix_path + ".pdf", bbox_inches="tight")
    fig.savefig(matrix_path + ".png", dpi=400, bbox_inches="tight")
    plt.close(fig)

    # Build a compact per-landmark table for downstream statistical analysis.
    x            = data_interest[variable]
    ys           = data_interest[distance_cols]
    corr_vals    = ys.corrwith(x, method="pearson")
    p_vals, note = _try_pearson_pvalues_from_r(corr_vals.values, n=len(data_interest))

    table        = pd.DataFrame(
        {
            "distance_col": corr_vals.index,
            "correlation": corr_vals.values,
            "n": int(len(data_interest)),
        }
    )
    if p_vals is not None:
        table["p_value"] = p_vals
        table["q_value_fdr_bh"] = _bh_fdr(p_vals)
    else:
        table["p_value"] = np.nan
        table["q_value_fdr_bh"] = np.nan

    table["abs_correlation"] = table["correlation"].abs()
    table                    = table.sort_values(["abs_correlation", "distance_col"], ascending=[False, True]).drop(columns=["abs_correlation"])
    table_path               = _corr_table_path_from_matrix_path(matrix_path)
    _ensure_parent_dir(table_path)
    table.to_csv(table_path, index=False)
    if note:
        print(note)
    print("Correlation table saved to", table_path)

def correlations_existing_variable(filename, variable, matrix_path):
    """
    Backwards-compatible wrapper for :func:`correlate_against_variable`.

    Notes
    -----
    Kept to avoid breaking existing notebooks/scripts that call
    ``correlations_existing_variable(...)``. Prefer :func:`correlate_against_variable`
    in new code.
    """
    return correlate_against_variable(filename=filename, variable=variable, matrix_path=matrix_path)

def correlate_against_listing_count(filename, variable, matrix_path):
    """
    Compute correlations between listing frequency and all distance columns.

    Parameters
    ----------
    filename : str
        Path to the CSV dataset containing host information and distance columns.
    variable : str
        Categorical or identifier variable used to derive ``listing_count``.
    matrix_path : str
        Base path used to export the correlation matrix and summary table.

    Notes
    -----
    The function first creates a derived variable called ``listing_count``,
    which counts how many rows share the same value of ``variable``.
    """
    # Load the dataset and validate that the requested grouping variable exists.
    data = _read_csv_with_fallback(filename)

    if variable not in data.columns:
        raise ValueError(f"Variable '{variable}' not found in data columns.")

    # Derive the number of listings associated with each value of the selected variable.
    data                  = data.copy()
    data["listing_count"] = data[variable].map(data[variable].value_counts())
    
    # Select only the derived count variable and the generated distance columns.
    distance_cols = [c for c in data.columns if str(c).startswith("d_t_")]
    if not distance_cols:
        raise ValueError("No distance columns found (expected columns starting with 'd_t_').")

    data_interest = data[["listing_count"] + distance_cols].copy()
    for c in data_interest.columns:
        data_interest[c] = pd.to_numeric(data_interest[c], errors="coerce")
    # User-selected behavior: drop rows where listing_count or any distance is missing.
    data_interest = data_interest.dropna(subset=["listing_count"] + distance_cols)

    # Compute the full Pearson correlation matrix for visualization purposes.
    correlations  = data_interest.corr(method="pearson")

    # Export the heatmap in vector and raster formats.
    _ensure_parent_dir(matrix_path)
    fig = plt.figure(figsize=(30, 24))
    sns.heatmap(correlations, annot=True, fmt=".2f", cmap="YlGnBu", vmin=-1, vmax=1, cbar=True)
    plt.title("Correlation Matrix.")
    plt.xticks(rotation=90)
    plt.yticks(rotation=0)
    fig.tight_layout()
    fig.savefig(matrix_path + ".pdf", bbox_inches="tight")
    fig.savefig(matrix_path + ".png", dpi=400, bbox_inches="tight")
    plt.close(fig)

    # Build a compact per-landmark table for downstream statistical analysis.
    x            = data_interest["listing_count"]
    ys           = data_interest[distance_cols]
    corr_vals    = ys.corrwith(x, method="pearson")
    p_vals, note = _try_pearson_pvalues_from_r(corr_vals.values, n=len(data_interest))

    table = pd.DataFrame(
        {
            "distance_col": corr_vals.index,
            "correlation": corr_vals.values,
            "n": int(len(data_interest)),
        }
    )
    if p_vals is not None:
        table["p_value"]        = p_vals
        table["q_value_fdr_bh"] = _bh_fdr(p_vals)
    else:
        table["p_value"]        = np.nan
        table["q_value_fdr_bh"] = np.nan

    table["abs_correlation"] = table["correlation"].abs()
    table      = table.sort_values(["abs_correlation", "distance_col"], ascending=[False, True]).drop(columns=["abs_correlation"])
    table_path = _corr_table_path_from_matrix_path(matrix_path)
    _ensure_parent_dir(table_path)
    table.to_csv(table_path, index=False)
    if note:
        print(note)
    print("Correlation table saved to", table_path)

def correlations_new_variable(filename, variable, matrix_path):
    """
    Backwards-compatible wrapper for :func:`correlate_against_listing_count`.

    Notes
    -----
    Kept to avoid breaking existing notebooks/scripts that call
    ``correlations_new_variable(...)``. Prefer :func:`correlate_against_listing_count`
    in new code.
    """
    return correlate_against_listing_count(filename=filename, variable=variable, matrix_path=matrix_path)

# Run the script as a standalone application
if __name__ == "__main__":
    # Distances calculation
    hosts           = "Information/hosts.csv"
    cultural_places = "Information/cultural_places.csv"
    cultural_result = "Results/hosts_with_distances_cultural.csv"
    distances(hosts, cultural_places, cultural_result)

    # Correlations First Test
    variable_1      = "host_is_superhost"
    matrix_path_1   = "Results/Correlation_Matrix_1"
    correlate_against_variable(cultural_result, variable_1, matrix_path_1)

    # Correlations Second Test
    variable_2      = "host_total_listings_count"
    matrix_path_2   = "Results/Correlation_Matrix_2"
    correlate_against_variable(cultural_result, variable_2, matrix_path_2)

    # Correlations Third Test
    variable_3      = "host_id"
    matrix_path_3   = "Results/Correlation_Matrix_3"
    correlate_against_listing_count(cultural_result, variable_3, matrix_path_3)
