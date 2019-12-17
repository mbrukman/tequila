from tequila import TequilaException, BitNumbering
from tequila.circuit.circuit import QCircuit
from tequila.utils.keymap import KeyMapSubregisterToRegister
from tequila.wavefunction.qubit_wavefunction import QubitWaveFunction
from tequila.circuit.compiler import change_basis
from tequila.circuit.gates import Measurement
from tequila import BitString
from tequila.objective import Objective, ExpectationValue
from tequila.simulators.heralding import HeraldingABC
from tequila.circuit import compiler
from tequila.circuit._gates_impl import MeasurementImpl

import numpy, numbers, typing, copy
from dataclasses import dataclass


@dataclass
class SimulatorReturnType:
    abstract_circuit: QCircuit = None
    circuit: int = None
    wavefunction: QubitWaveFunction = None
    measurements: typing.Dict[str, QubitWaveFunction] = None
    backend_result: int = None

    @property
    def counts(self, key: str = None):
        if self.measurements is not None:
            if key is None:
                keys = [k for k in self.measurements.keys()]
                return self.measurements[keys[0]]
            else:
                return self.measurements[key]
        elif self.wavefunction is not None:
            measurement = copy.deepcopy(self.wavefunction)
            for k, v in measurement.items():
                measurement[k] = numpy.abs(v) ** 2
            return measurement


class BackendHandler:
    """
    This needs to be overwritten by all supported Backends
    """

    recompile_trotter = True
    recompile_swap = False
    recompile_multitarget = True
    recompile_controlled_rotation = False
    recompile_exponential_pauli = True

    def recompile(self, abstract_circuit: QCircuit) -> QCircuit:
        # order matters!
        recompiled = abstract_circuit
        if self.recompile_trotter:
            recompiled = compiler.compile_trotterized_gate(gate=recompiled,
                                                           compile_exponential_pauli=self.recompile_exponential_pauli)
        if self.recompile_exponential_pauli:
            recompiled = compiler.compile_exponential_pauli_gate(gate=recompiled)
        if self.recompile_multitarget:
            recompiled = compiler.compile_multitarget(gate=recompiled)
        if self.recompile_controlled_rotation:
            recompiled = compiler.compile_controlled_rotation(gate=recompiled)
        if self.recompile_swap:
            recompiled = compiler.compile_swap(gate=recompiled)

        return recompiled

    def fast_return(self, abstract_circuit):
        return True

    def initialize_circuit(self, qubit_map, *args, **kwargs):
        TequilaException("Backend Handler needs to be overwritten for supported simulators")

    def add_gate(self, gate, circuit, qubit_map, *args, **kwargs):
        TequilaException("Backend Handler needs to be overwritten for supported simulators")

    def add_controlled_gate(self, gate, qubit_map, circuit, *args, **kwargs):
        return self.add_gate(gate, circuit, args, kwargs)

    def add_rotation_gate(self, gate, qubit_map, circuit, *args, **kwargs):
        return self.add_gate(gate, circuit, args, kwargs)

    def add_controlled_rotation_gate(self, gate, qubit_map, circuit, *args, **kwargs):
        return self.add_gate(gate, circuit, args, kwargs)

    def add_power_gate(self, gate, qubit_map, circuit, *args, **kwargs):
        return self.add_gate(gate, circuit, args, kwargs)

    def add_controlled_power_gate(self, gate, qubit_map, circuit, *args, **kwargs):
        return self.add_gate(gate, circuit, args, kwargs)

    def add_measurement(self, gate, qubit_map, circuit, *args, **kwargs):
        TequilaException("Backend Handler needs to be overwritten for supported simulators")

    def make_qubit_map(self, abstract_circuit: QCircuit):
        return [i for i in range(len(abstract_circuit.qubits))]


class SimulatorBase:
    """
    Abstract Base Class for OpenVQE interfaces to simulators
    """
    numbering: BitNumbering = BitNumbering.MSB
    backend_handler = BackendHandler()

    def __init__(self, heralding: HeraldingABC = None, ):
        self._heralding = heralding
        self.__decompose_and_compile = True

    def __call__(self, objective: typing.Union[QCircuit, Objective, ExpectationValue], samples: int = None, **kwargs) -> numbers.Real:
        """
        :param objective: Objective or simple QCircuit
        :param samples: Number of Samples to evaluate, None means full wavefunction simulation
        :param kwargs: keyword arguments to further pass down
        :return: Energy, or simulator return type depending on what was passed down as objective
        """

        if isinstance(objective, QCircuit):
            if samples is None:
                return self.simulate_wavefunction(abstract_circuit=objective)
            else:
                return self.run(abstract_circuit=objective, samples=samples)
        elif isinstance(objective, ExpectationValue):
            if samples is None:
                return self.simulate_expectationvalue(E=objective)
            else:
                NotImplementedError("not here yet")
        else:
            if samples is None:
                return self.simulate_objective(objective=objective)
            else:
                return self.measure_objective(objective=objective, samples=samples, **kwargs)

    def run(self, abstract_circuit: QCircuit, samples: int = 1) -> SimulatorReturnType:
        circuit = self.create_circuit(abstract_circuit=abstract_circuit)
        backend_result = self.do_run(circuit=circuit, samples=samples)
        return SimulatorReturnType(circuit=circuit,
                                   abstract_circuit=abstract_circuit,
                                   backend_result=backend_result,
                                   measurements=self.postprocessing(self.convert_measurements(backend_result)))

    def do_run(self, circuit, samples: int = 1):
        raise TequilaException("do_run needs to be overwritten by corresponding backend")

    def set_compile_flag(self, b):
        self.__decompose_and_compile = b

    def simulate_wavefunction(self, abstract_circuit: QCircuit, returntype=None,
                              initial_state: int = 0) -> SimulatorReturnType:
        """
        Simulates an abstract circuit with the backend specified by specializations of this class
        :param abstract_circuit: The abstract circuit
        :param returntype: specifies how the result should be given back
        :param initial_state: The initial state of the simulation,
        if given as an integer this is interpreted as the corresponding multi-qubit basis state
        :return: The resulting state
        """

        if isinstance(initial_state, BitString):
            initial_state = initial_state.integer
        if isinstance(initial_state, QubitWaveFunction):
            if len(initial_state.keys()) != 1:
                raise TequilaException("only product states as initial states accepted")
            initial_state = list(initial_state.keys())[0].integer

        active_qubits = abstract_circuit.qubits
        all_qubits = [i for i in range(abstract_circuit.n_qubits)]

        # maps from reduced register to full register
        keymap = KeyMapSubregisterToRegister(subregister=active_qubits, register=all_qubits)

        result = self.do_simulate_wavefunction(abstract_circuit=abstract_circuit, initial_state=keymap.inverted(initial_state).integer)
        result.wavefunction.apply_keymap(keymap=keymap, initial_state=initial_state)
        return result

    def do_simulate_wavefunction(self, circuit, initial_state=0) -> SimulatorReturnType:
        raise TequilaException(
            "called from base class of simulators, or non-supported operation for this backend")

    def create_circuit(self, abstract_circuit: QCircuit):
        """
        Translates abstract circuits into the specific backend type
        Overwrite the BackendHandler Class to implement new backends
        :param abstract_circuit: Abstract circuit to be translated
        :return: translated circuit
        """

        if self.__decompose_and_compile:
            decomposed_ac = abstract_circuit.decompose()
            decomposed_ac = self.backend_handler.recompile(abstract_circuit=decomposed_ac)
        else:
            decomposed_ac = abstract_circuit

        if self.backend_handler.fast_return(decomposed_ac):
            return decomposed_ac

        qubit_map = self.backend_handler.make_qubit_map(abstract_circuit=decomposed_ac)

        result = self.backend_handler.initialize_circuit(qubit_map=qubit_map)

        for g in decomposed_ac.gates:
            if isinstance(g, MeasurementImpl):
                self.backend_handler.add_measurement(gate=g, qubit_map=qubit_map, circuit=result)
            elif g.is_controlled():
                if hasattr(g, "angle"):
                    self.backend_handler.add_controlled_rotation_gate(gate=g, qubit_map=qubit_map, circuit=result)
                elif hasattr(g, "power") and g.power != 1:
                    self.backend_handler.add_controlled_power_gate(gate=g, qubit_map=qubit_map, circuit=result)
                else:
                    self.backend_handler.add_controlled_gate(gate=g, qubit_map=qubit_map, circuit=result)
            else:
                if hasattr(g, "angle"):
                    self.backend_handler.add_rotation_gate(gate=g, qubit_map=qubit_map, circuit=result)
                elif hasattr(g, "power") and g.power != 1:
                    self.backend_handler.add_power_gate(gate=g, qubit_map=qubit_map, circuit=result)
                else:
                    self.backend_handler.add_gate(gate=g, qubit_map=qubit_map, circuit=result)

        return result

    def convert_measurements(self, backend_result) -> typing.Dict[str, QubitWaveFunction]:
        raise TequilaException(
            "called from base class of simulators, or non-supported operation for this backend")

    def measure_expectationvalue(self, E: ExpectationValue,samples: int,return_simulation_data: bool = False) -> numbers.Real:
        H = E.H
        U = E.U
        # The hamiltonian can be defined on more qubits as the unitaries
        result_data = {}
        final_E = 0.0
        for ps in H.paulistrings:
            Etmp, tmp = self.measure_paulistring(abstract_circuit=U, paulistring=ps, samples=samples)
            final_E += Etmp
            result_data[str(ps)] = tmp

        # type conversion to not confuse optimizers
        if hasattr(final_E, "imag"):
            assert(numpy.isclose(final_E.imag, 0.0))
            final_E = float(final_E.real)

        if return_simulation_data:
            return final_E, result_data
        else:
            return final_E

    def measure_objective(self, objective: Objective, samples: int, return_simulation_data: bool = False) -> float:
        elist = []
        data = []
        for ex in objective.expectationvalues:
            result_data = {}
            evalue=0.0
            for ps in ex.H.paulistrings:
                Etmp, tmp = self.measure_paulistring(abstract_circuit=ex.U, paulistring=ps, samples=samples)
                evalue += Etmp
                result_data[str(ps)] = tmp
            elist.append(evalue)
            if return_simulation_data:
                data.append(tmp)

        # in principle complex weights are allowed, but it probably will never occur
        # however, for now here is the type conversion to not confuse optimizers
        final_E=objective.transformation(*elist)
        if hasattr(final_E, "imag") and numpy.isclose(final_E.imag, 0.0):
            final_E = float(final_E.real)

        if return_simulation_data:
            return final_E, data
        else:
            return final_E

    def simulate_expectationvalue(self, E: ExpectationValue, return_simulation_data: bool = False) -> numbers.Real:
        final_E = 0.0
        data = []
        H = E.H
        U = E.U
        # The hamiltonian can be defined on more qubits as the unitaries
        qubits_h = H.qubits
        qubits_u = U.qubits
        all_qubits = list(set(qubits_h) | set(qubits_u))
        keymap = KeyMapSubregisterToRegister(subregister=qubits_u, register=all_qubits)
        simresult = self.simulate_wavefunction(abstract_circuit=U)
        wfn = simresult.wavefunction.apply_keymap(keymap=keymap)
        final_E += wfn.compute_expectationvalue(operator=H)
        if return_simulation_data:
            data.append(simresult)

        # type conversion to not confuse optimizers
        if hasattr(final_E, "imag"):
            assert(numpy.isclose(final_E.imag, 0.0))
            final_E = float(final_E.real)

        if return_simulation_data:
            return final_E, data
        else:
            return final_E

    def simulate_objective(self, objective: Objective):
        # simulate all expectation values
        # TODO easy to parallelize
        E = []
        for Ei in objective._expectationvalues:
            E.append(self.simulate_expectationvalue(E=Ei))
        # return evaluated result
        return objective.transformation(*E)


    def measure_paulistring(self, abstract_circuit: QCircuit, paulistring, samples: int = 1):
        # make basis change
        basis_change = QCircuit()
        for idx, p in paulistring.items():
            basis_change += change_basis(target=idx, axis=p)
        # make measurement instruction
        measure = QCircuit()
        qubits = [idx[0] for idx in paulistring.items()]
        if len(qubits) == 0:
            # no measurement instructions for a constant term as paulistring
            return (paulistring.coeff, SimulatorReturnType())
        else:
            measure += Measurement(name=str(paulistring), target=qubits)
            circuit = abstract_circuit + basis_change + measure
            # run simulators
            sim_result = self.run(abstract_circuit=circuit, samples=samples)

            # compute energy
            counts = sim_result.counts
            E = 0.0
            n_samples = 0
            for key, count in counts.items():
                parity = key.array.count(1)
                sign = (-1) ** parity
                E += sign * count
                n_samples += count
            if self._heralding is None:
                assert (n_samples == samples)  # failsafe
            E = E / samples * paulistring.coeff
            return (E, sim_result)

    def postprocessing(self, measurements: typing.Dict[str, QubitWaveFunction]) -> typing.Dict[str, QubitWaveFunction]:
        # fast return
        if self._heralding is None:
            return measurements

        result = dict()
        for k, v in measurements.items():
            result[k] = self._heralding(input=v)
        return result