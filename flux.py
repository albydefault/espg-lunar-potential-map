import numpy as np
import pandas as pd
import logging
from scipy.optimize import minimize
from scipy.stats.qmc import LatinHypercube, scale
from typing import Tuple, Union

from model import synth_losscone
import config

logger = logging.getLogger(__name__)


class ERData:
    def __init__(self, er_data_file: str):
        """
        Initialize the ERData class with the path to the ER data file.
        """
        self.er_data_file = er_data_file
        self.data = None

        self._load_data()

    def _load_data(self) -> None:
        """
        Load the ER data from the specified file.

        Reads the specified file into a pandas DataFrame, using the column names defined in ALL_COLS.
        If the file is not found, or if there is an error parsing the file, the data attribute is set to None.
        """
        # Read the data file
        try:
            self.data = pd.read_csv(self.er_data_file, sep=r"\s+", engine="python", header=None, names=config.ALL_COLS)
            self._clean_sweep_data()
        except FileNotFoundError:
            logger.error(f"Error: The file {self.er_data_file} was not found.")
            self.data = None
        except pd.errors.ParserError:
            logger.error(f"Error: The file {self.er_data_file} could not be parsed. Please check the file format.")
            self.data = None
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
            self.data = None

    def _clean_sweep_data(self) -> None:
        """
        Remove entire sweeps that contain any invalid rows.

        Identifies sweeps with invalid timestamps or magnetic field data,
        then removes all rows belonging to those spec_no values.
        """
        if self.data is None:
            return

        original_rows = len(self.data)

        # Identify invalid rows
        magnetic_field = self.data[config.MAG_COLS].to_numpy(dtype=np.float64)
        magnetic_field_magnitude = np.linalg.norm(magnetic_field, axis=1)

        invalid_mag_mask = (magnetic_field_magnitude <= 1e-9) | (magnetic_field_magnitude >= 1e3)
        invalid_time_mask = self.data['time'] == '1970-01-01T00:00:00'
        invalid_rows_mask = invalid_mag_mask | invalid_time_mask

        # Get spec_no values for invalid rows
        invalid_spec_nos = set(self.data.loc[invalid_rows_mask, 'spec_no'].values)

        if invalid_spec_nos:
            logger.info(f"Removing {len(invalid_spec_nos)} sweeps with invalid data")

            # Remove all rows belonging to invalid spec_nos
            valid_mask = ~self.data['spec_no'].isin(list(invalid_spec_nos))
            self.data = self.data[valid_mask].reset_index(drop=True)

            removed_rows = original_rows - len(self.data)
            logger.info(f"Removed {removed_rows} rows ({removed_rows/original_rows*100:.1f}%) from {len(invalid_spec_nos)} invalid sweeps")

class PitchAngle:
    """
    Initialize the PitchAngle class with the ER data and theta values.

    Data rows with invalid B-field are retained; all such rows are flagged via valid_mask and their derived quantities are NaN. Down-stream algorithms must honor this mask.

    Attributes:
        er_data: The ER data object.
        thetas: The theta values in degrees.
        cartesian_coords: The Cartesian coordinates of the data points.
        pitch_angles: The pitch angles in degrees.
        unit_magnetic_field: The unit magnetic field vectors.
        valid_mask: A mask indicating valid data points.
    """

    def __init__(self, er_data: ERData, thetas: str):
        """
        Initialize the PitchAngle class with the ER data and theta values.

        Args:
            er_data (ERData): The ER data object.
            thetas (str): The path to the theta values file.
        """
        self.er_data = er_data
        self.thetas = np.loadtxt(thetas, dtype=np.float64) # Expects theta values in degrees
        self.cartesian_coords: Union[np.ndarray, None] = None
        self.pitch_angles: Union[np.ndarray, None] = None
        self.unit_magnetic_field: Union[np.ndarray, None] = None
        self.valid_mask: Union[np.ndarray, None] = None

        self._process_data()

    def _get_cartesian_coords(self, phis: np.ndarray, thetas: np.ndarray) -> np.ndarray:
        """
        Convert spherical coordinates (phi, theta) to Cartesian coordinates (X, Y, Z).

        Args:
            phis (np.ndarray): The phi values in radians.
            thetas (np.ndarray): The theta values in radians.

        Returns:
            np.ndarray: The Cartesian coordinates (X, Y, Z).
        """
        X: np.ndarray = np.cos(phis) * np.cos(thetas)
        Y: np.ndarray = np.sin(phis) * np.cos(thetas)
        z_base: np.ndarray = np.sin(thetas)
        Z: np.ndarray = np.broadcast_to(z_base, X.shape)
        return np.stack((X, Y, Z), axis=-1)

    def _process_data(self):
        """
        Process the ER data to calculate the Cartesian coordinates and prepare
        the unit magnetic field vectors for pitch angle calculation.

        This function performs data validation and transformation from spherical
        to Cartesian coordinates. It also normalizes the magnetic field vectors
        and stores indices of valid and invalid data points.

        Args:
            None

        Returns:
            None
        """
        # Check if data is loaded
        assert self.er_data.data is not None, "Data not loaded. Please load the data first."
        assert len(self.thetas) == config.CHANNELS, f"Theta values must match the number of channels {config.CHANNELS}."

        # Convert spherical coordinates (phi, theta) to Cartesian coordinates (X, Y, Z)
        phis: np.ndarray = np.deg2rad(self.er_data.data[config.PHI_COLS].to_numpy(dtype=np.float64))
        thetas: np.ndarray = np.deg2rad(self.thetas)

        # Calculate Cartesian coordinates
        self.cartesian_coords: np.ndarray = self._get_cartesian_coords(phis, thetas)

        # Extract magnetic field vectors and calculate their magnitudes
        magnetic_field: np.ndarray = self.er_data.data[config.MAG_COLS].to_numpy(dtype=np.float64)
        magnetic_field_magnitude: np.ndarray = np.linalg.norm(magnetic_field, axis=1, keepdims=True)

        # Since data is pre-cleaned at sweep level, all remaining rows should be valid
        # Just normalize the magnetic field vectors directly
        unit_magnetic_field: np.ndarray = magnetic_field / magnetic_field_magnitude
        self.valid_mask: np.ndarray = np.ones(len(magnetic_field), dtype=bool)

        # Tile the unit magnetic field vectors for pitch angle calculation
        unit_magnetic_field: np.ndarray = np.tile(unit_magnetic_field[:, None, :], (1, config.CHANNELS, 1))
        self.unit_magnetic_field: np.ndarray = unit_magnetic_field

        # Calculate the pitch angles
        self.calculate_pitch_angles()

    def calculate_pitch_angles(self) -> None:
        """
        Calculate the pitch angles based on the loaded ER data and theta values.

        The pitch angle is the angle between the magnetic field line and the radial direction.
        It is calculated as the arccosine of the dot product between the unit magnetic field
        vector and the radial direction vector.

        Returns:
            None
        """
        # Check if data is loaded
        assert self.er_data.data is not None, "Data not loaded. Please load the data first."

        # Calculate the pitch angles
        # The dot product between the unit magnetic field vector and the radial direction vector
        # is the cosine of the pitch angle.
        # Negative sign is used because the radial direction vector is meant to point towards the sensor.
        dot_product: np.ndarray = -np.einsum('ijk,ijk->ij', self.unit_magnetic_field, self.cartesian_coords)
        # Clip the dot product to ensure it is in the range [-1, 1]
        dot_product = np.clip(dot_product, -1, 1)

        # Calculate the pitch angles using the arccosine function
        pitch_angles: np.ndarray = np.arccos(dot_product)
        # Convert the pitch angles to degrees
        pitch_angles = np.rad2deg(pitch_angles)

        # Store the pitch angles in the class attribute
        self.pitch_angles = pitch_angles

class LossConeFitter:
    def __init__(self, er_data: ERData, thetas: str):
        """
        Initialize the LossConeFitter class with the ER data and theta values.

        Args:
            er_data (ERData): The ER data object.
            thetas (str): The path to the theta values file.
        """
        self.er_data: ERData = er_data
        self.thetas: np.ndarray = np.loadtxt(thetas, dtype=np.float64)
        self.pitch_angle: PitchAngle = PitchAngle(er_data, thetas)

        self.lhs: np.ndarray = self._generate_latin_hypercube()

    def _generate_latin_hypercube(self) -> np.ndarray:
        """
        Generate a Latin Hypercube sample.

        Returns:
            np.ndarray: The Latin Hypercube sample.
        """
        # Generate a Latin Hypercube sample
        bounds: np.ndarray = np.array([[-1000.0,  0.1],   # lower
                                       [ 1000.0, 1.0]])
        sampler = LatinHypercube(d=2, scramble=False)
        lhs: np.ndarray = sampler.random(n=400)            # 400 points

        return scale(lhs, bounds[0], bounds[1])


    def _get_normalized_flux(self, energy_bin: int, measurement_chunk: int) -> np.ndarray:
        """
        Get the normalized flux for a specific energy bin and measurement chunk.

        Args:
            energy_bin (int): The index of the energy bin.
            measurement_chunk (int): The index of the measurement chunk.

        Returns:
            np.ndarray: The normalized flux for the specified energy bin and measurement chunk.
        """
        # Check if data is loaded
        assert self.er_data.data is not None, "Data not loaded. Please load the data first."

        # Get the electron flux for the specified energy bin and measurement chunk
        index = measurement_chunk * config.SWEEP_ROWS + energy_bin

        # Data is pre-cleaned, so just check bounds
        if index >= len(self.er_data.data):
            return np.full(config.CHANNELS, np.nan)


        electron_flux: np.ndarray = self.er_data.data[config.FLUX_COLS].to_numpy(dtype=np.float64)[index]
        if self.pitch_angle.pitch_angles is None:
            return np.full(config.CHANNELS, np.nan)
        angles: np.ndarray = self.pitch_angle.pitch_angles[index]
        incident_mask: np.ndarray = angles < 90
        reflected_mask: np.ndarray = ~incident_mask

        # Check if the electron flux is valid
        if not incident_mask.any():
            return np.full_like(electron_flux, np.nan)

        # Get the angles and fluxes for the incident and reflected regions
        incident_flux: float = float(max(config.EPS, np.mean(electron_flux[incident_mask])))
        normalized_flux: np.ndarray = electron_flux / incident_flux

        # It's weird to normalize only the reflected flux
        # Combine the incident and reflected fluxes
        # combined_flux: np.ndarray = np.zeros_like(electron_flux)
        # combined_flux[incident_mask] = electron_flux[incident_mask]
        # combined_flux[reflected_mask] = normalized_flux
        return normalized_flux


    def build_norm2d(self, measurement_chunk: int) -> np.ndarray:
        """
        Build a 2D normalized flux distribution for a specific measurement chunk.

        Args:
            measurement_chunk (int): The index of the measurement chunk.

        Returns:
            np.ndarray: The 2D normalized flux distribution for the specified measurement chunk.
        """
        # Check if data is loaded
        assert self.er_data.data is not None, "Data not loaded. Please load the data first."

        norm2d: np.ndarray = np.vstack([
            self._get_normalized_flux(energy_bin, measurement_chunk)
            for energy_bin in range(config.SWEEP_ROWS)
        ])

        return norm2d

    def _fit_surface_potential(self, measurement_chunk: int) -> Tuple[float, float, float]:
        """
        Fit surface potential (ΔU) and B_s/B_m for one 15-row measurement chunk
        using χ² minimisation with scipy.optimize.minimize.

        Returns
        -------
        delta_U   : best-fit surface potential in volts
        bs_over_bm: best-fit B_s/B_m ratio
        chi2      : final χ² value
        """
        assert self.er_data.data is not None, "Data not loaded."

        # --- prepare data for the chunk -------------------------------------------------
        eps: float = 1e-6
        norm2d: np.ndarray = self.build_norm2d(measurement_chunk)

        # Check if we have valid data
        if np.isnan(norm2d).all():
            return np.nan, np.nan, np.nan

        s: int = measurement_chunk * config.SWEEP_ROWS
        e: int = (measurement_chunk + 1) * config.SWEEP_ROWS

        # Ensure indices are within bounds
        max_rows = len(self.er_data.data)
        if s >= max_rows:
            return np.nan, np.nan, np.nan
        e = min(e, max_rows)

        energies: np.ndarray = self.er_data.data["energy"].to_numpy(dtype=np.float64)[s:e]

        # Ensure pitch angles exist for this range
        if self.pitch_angle.pitch_angles is None or s >= len(self.pitch_angle.pitch_angles):
            return np.nan, np.nan, np.nan
        pitches: np.ndarray = self.pitch_angle.pitch_angles[s:e]

        # Adjust norm2d size if needed
        actual_rows = e - s
        if norm2d.shape[0] > actual_rows:
            norm2d = norm2d[:actual_rows]

        # objective ---------------------------------------------------------------------
        def chi2(params):
            delta_U, bs_over_bm = params
            model = synth_losscone(energies, pitches, delta_U, bs_over_bm)

            if not np.all(np.isfinite(model)) or (model <= 0).all():
                return 1e30 # big penalty

            diff = np.log(norm2d + eps) - np.log(model + eps)
            return np.sum(diff * diff)
        # -------------------------------------------------------------------------------
        # 1) Latin-hypercube global scan (20×20 ≈ 400 evaluations)
        # -------------------------------------------------------------------------------
        chi2_vals: np.ndarray = np.apply_along_axis(chi2, 1, self.lhs)
        best_idx: int = int(np.argmin(chi2_vals))
        x0: np.ndarray = self.lhs[best_idx]               # ΔU, Bₛ/Bₘ for local start
        # -------------------------------------------------------------------------------
        # 2) Local Nelder–Mead refinement
        # -------------------------------------------------------------------------------
        result = minimize(
            chi2, x0,
            method="Nelder-Mead",
            options=dict(maxiter=1000, xatol=1e-4, fatol=1e-4)
        )
        if not result.success:
            raise RuntimeError(f"Optimisation failed: {result.message}")
        delta_U: float = result.x[0]
        bs_over_bm: float = result.x[1]
        return float(delta_U), float(bs_over_bm), float(result.fun)

    def fit_surface_potential(self) -> np.ndarray:
        """
        Fit surface potential (ΔU) and B_s/B_m for all 15-row measurement chunks
        using χ² minimisation with scipy.optimize.minimize.

        Returns
        -------
        delta_U   : best-fit surface potential in volts
        bs_over_bm: best-fit B_s/B_m ratio
        chi2      : final χ² value
        """
        assert self.er_data.data is not None, "Data not loaded."

        # Fit for each chunk
        n_chunks: int = len(self.er_data.data) // config.SWEEP_ROWS
        results: np.ndarray = np.zeros((n_chunks, 4))
        for i in range(n_chunks):
            delta_U, bs_over_bm, chi2 = self._fit_surface_potential(i)
            results[i] = [delta_U, bs_over_bm, chi2, i]

        return results

class FluxData:
    def __init__(self, er_data_file: str, thetas: str):
        """
        Initialize the FluxData class as an orchestrator using the new class structure.

        Args:
            er_data_file (str): Path to the ER data file
            thetas (str): Path to the theta values file
        """
        # Use the new class structure
        self.er_data = ERData(er_data_file)
        self.pitch_angle = PitchAngle(self.er_data, thetas)
        self.loss_cone_fitter = LossConeFitter(self.er_data, thetas)

        # Expose data for backward compatibility
        self.data = self.er_data.data


    def load_data(self):
        """
        Load the ER data from the specified file.
        Deprecated: Data loading is now handled by ERData class.
        """
        pass

    def process_data(self):
        """
        Process the loaded data to calculate coordinates and pitch angles.
        Deprecated: Data processing is now handled by PitchAngle class.
        """
        pass

    def get_normalized_flux(self, energy_bin: int, measurement_chunk: int) -> np.ndarray:
        """
        Get the normalized flux for a specific energy bin and measurement chunk.
        Delegates to LossConeFitter.
        """
        return self.loss_cone_fitter._get_normalized_flux(energy_bin, measurement_chunk)

    def build_norm2d(self, measurement_chunk: int):
        """
        Build a 2D normalized flux distribution for a specific measurement chunk.
        Delegates to LossConeFitter.
        """
        return self.loss_cone_fitter.build_norm2d(measurement_chunk)

    def _fit_surface_potential(self, measurement_chunk: int):
        """
        Fit surface potential for one measurement chunk.
        Delegates to LossConeFitter.
        """
        return self.loss_cone_fitter._fit_surface_potential(measurement_chunk)

    def fit_surface_potential(self):
        """
        Fit surface potential for all measurement chunks.
        Delegates to LossConeFitter.
        """
        return self.loss_cone_fitter.fit_surface_potential()
