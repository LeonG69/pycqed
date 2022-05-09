import qutip as qt
import numpy as np
import sympy as sy
import scipy as sc
import networkx as nx
import time

from . import dataspec as ds
from . import symbolic_system as cs
from . import parameters as pa
from . import units as un
from . import physical_constants as pc
from . import util
from . import units

class NumericalSystem(ds.TempData):
    
    ## Available basis representations
    __basis_repr = [
        "charge", 
        "flux", 
        "oscillator", 
        "custom"
    ]
    
    ## Mode types
    __mode_types = [
        "osc", # DoFs have capacitive and inductive parts only
        "jos", # DoFs have capacitive, inductive and Josephson parts
        "isl", # DoFs have capacitive parts only
        "leg"  # DoFs have inductive parts only
    ]
    
    ## FIXME: Consider listing dependencies here too
    __eval_avail = [
        "getHamiltonian", 
        "getBranchCurrents", 
        "getNodeVoltages", 
        "getChargingEnergies", 
        "getJosephsonEnergies", 
        "getResonatorResponse"
    ]
    
    ## Initialise a Hamiltonian using a circuit specification
    def __init__(self, symbolic_system, unit=units.Units("CQED1")):
        
        # Initialise the temporary data manager
        super().__init__()
        self.newSession(id(self))
        self.__use_temp = False
        
        # Assign the circuit
        self.SS = symbolic_system
        
        # Nested dictionary for DoF operators, keyed by the relevant mode
        self.circ_operators = {}
        self.operator_data = {}
        
        # Mode truncations and basis representation, also indicates whether
        # operators need to be considered for regeneration when parameters are changed.
        self.mode_truncations = {}
        
        # Define the parameter collection
        # FIXME: Symbols to be integrated in the ParamCollection class
        self.all_params_sym = self.SS.getParameterDict()
        self.params = pa.ParamCollection(list(self.all_params_sym.keys()))
        
        # For parameterisations
        self.params_subs = {}
        
        # Coordinate transformation matrix
        self.Rmat = 1.0
        self.RmatT = 1.0
        self.Rmatinv = 1.0
        self.RmatTinv = 1.0
        
        # Oscillator characteristics
        self.osc_params = {}
        
        # Set the unit system
        self.units = unit
        self._set_parameter_units()
        
        # Load default diagonaliser configuration
        self.setDiagConfig()
    
    # Called when deleting
    def __del__(self):
        self.clearSessionData()
    
    ###################################################################################################################
    #       Operator Generation and Functions
    ###################################################################################################################
    
    def getNodeList(self):
        return self.SS.nodes
    
    def getNodeIndex(self, node):
        return self.SS.nodes.index(node)
        
    def getCommutator(self, Q, P, basis="charge"):
        if basis == "charge": # Need to figure out the correction for this case
            dims = 2*pc.e*pc.phi0/pc.hbar
            return qt.commutator(P, Q)*dims
        elif basis == "oscillator":
            
            # Get highest number operator eigenvalue and associated eigenstate
            enum = Q.shape[0]
            vnum = qt.basis(enum, enum-1)
            
            # Create the correction matrix
            mat = 1 - (enum)*vnum*vnum.dag()
            
            # Invert it
            corrmat = qt.Qobj(np.linalg.inv(mat.data.todense()))
            
            # Get the correct commutator
            return qt.commutator(P, Q)*corrmat
    
    def setTruncation(self, pos, trunc, basis):
        """ Sets the truncation of the operators associated with the given branch or node.
        
        For charge and flux basis types, the truncation will be set to `2*trunc+1` to account for negative offsets of neutral charge or flux.
        
        :param pos: The node or branch.
        :type pos: int, tuple
        
        :param trunc: The truncation.
        :type trunc: int
        
        :param basis: The basis representation string, can be one defined in the `__basis_repr` private variable of this class.
        :type basis: str
        
        :return: None
        """
        if pos not in self.getNodeList():
            raise Exception("pos '%s' does not exist." % repr(pos))
        self.mode_truncations[pos] = (trunc, basis, True if basis == "oscillator" else False)
    
    # FIXME: Implement me
    def setTruncations(self):
        pass
    
    ## Get Hilbert space size
    def getHilbertSpaceSize(self):
        """ Returns the Hilbert space size considering all currently defined operator truncations.
        
        :return: The Hilbert space size
        :rtype: int
        """
        ret = 1
        for m in list(self.mode_truncations.keys()):
            trunc, basis, update = self.mode_truncations[m]
            if basis == "charge":
                ret *= (2*trunc+1)
            elif basis == "oscillator":
                ret *= (trunc)
        return ret
    
    def getOperatorList(self, node):
        Q = None
        P = None
        D = None
        Ddag = None
        trunc = self.operator_data[node]["truncation"]
        basis = self.operator_data[node]["basis"]
        impedance = self.operator_data[node]["impedance"]
        osc_impedance = None
        if impedance is not None:
            subs = {}
            for k, v in self.all_params_sym.items():
                subs[v] = self.params.getParameterValue(k)
            
            subs = self.SS.getSymbolValuesDict()
            
            osc_impedance = float(impedance.subs(subs))*self.units.getPrefactor("Impe")
            if osc_impedance != osc_impedance or osc_impedance == 0.0:
                raise Exception("Parameters not set for oscillator mode '%i'." % node)
        
        if basis == "charge":
            # For IJ modes the charge states are the eigenvectors of the following matrix
            q, s = (-qt.num(2*trunc + 1, trunc)).eigenstates()
            
            # Construct the flux states
            phik_list = []
            phik = None
            qm = [float(i)-trunc for i in range(2*trunc + 1)]
            for k in qm:
                phik = qt.basis(2*trunc + 1, 0) - 1
                for j, qi in enumerate(qm):
                    phik += np.sqrt(1/(2*trunc + 1)) * np.exp(2j*np.pi*k*qi/(2*trunc + 1))*s[j]
                phik_list.append(phik)
            
            # From this build the flux operator
            phik_eigvals = [(float(i)-trunc)/(2*trunc + 1) for i in range(2*trunc + 1)]
            P = qt.qeye(2*trunc + 1) - qt.qeye(2*trunc + 1)
            for i, phik in enumerate(phik_list):
                P += phik_eigvals[i]*phik*phik.dag()
            
            # Get a simple charge number operator
            Q = -qt.num(2*trunc + 1)+float(trunc)
            
            # Generate displacement operators by first diagonalising the flux operator
            E, V = np.linalg.eigh(P.data.todense())
            
            # Create transformation matrices
            U = qt.Qobj(V)
            Uinv = qt.Qobj(np.linalg.inv(V))
            
            # Exponentiate the diagonal matrix
            D = U*qt.Qobj(np.diag(np.exp(-2j*np.pi*E)))*Uinv - qt.basis(2*trunc+1, 2*trunc)*qt.basis(2*trunc+1, 0).dag()
            Ddag = D.dag()
            
        elif basis == "oscillator":
            # Using oscillator basis
            a = 1.0
            Q = 1j*np.sqrt(1/(2*osc_impedance))*(qt.create(trunc) - qt.destroy(trunc))*self.units.getPrefactor("ChgOsc")
            P = np.sqrt(osc_impedance/2)*(qt.create(trunc) + qt.destroy(trunc))*self.units.getPrefactor("FlxOsc")
            Pp = a*2*np.pi/pc.phi0*np.sqrt(pc.hbar)*P/self.units.getPrefactor("FlxOsc")
            
            # Generate displacement operators by first diagonalising the flux operator
            E, V = np.linalg.eigh(Pp.data.todense())
            
            # Create transformation matrices
            U = qt.Qobj(V)
            Uinv = qt.Qobj(np.linalg.inv(V))
            
            # Exponentiate the diagonal matrix
            D = U*qt.Qobj(np.diag(np.exp(1j*E)))*Uinv
            Ddag = D.dag()
        elif basis == "flux":
            # Flux operator is based on a flux grid
            pmax = self.operator_data[node]["flux_max"]
            grid = np.linspace(-pmax, pmax, 2*trunc+1)
            P = qt.Qobj(np.diag(grid))
            
            q, s = P.eigenstates()
            
            # Construct the flux states
            phik_list = []
            phik = None
            qm = [float(i)-trunc for i in range(2*trunc + 1)]
            for k in qm:
                phik = qt.basis(2*trunc + 1, 0) - 1
                for j, qi in enumerate(qm):
                    phik += np.sqrt(1/(2*trunc + 1)) * np.exp(2j*np.pi*k*qi/(2*trunc + 1))*s[j]
                phik_list.append(phik)

            # From this build the flux operator
            phik_eigvals = [(float(i)-trunc)/(2*trunc + 1) for i in range(2*trunc + 1)]
            Q = qt.qeye(2*trunc + 1) - qt.qeye(2*trunc + 1)
            for i, phik in enumerate(phik_list):
                Q += phik_eigvals[i]*phik*phik.dag()
            
            Q = Q/(grid[1]-grid[0])
            
            # Generate displacement operators by first diagonalising the flux operator
            E, V = np.linalg.eigh(P.data.todense())
            
            # Create transformation matrices
            U = qt.Qobj(V)
            Uinv = qt.Qobj(np.linalg.inv(V))
            
            # Exponentiate the diagonal matrix
            D = U*qt.Qobj(np.diag(np.exp(-2j*np.pi*E)))*Uinv - qt.basis(2*trunc+1, 2*trunc)*qt.basis(2*trunc+1, 0).dag()
            Ddag = D.dag()
            
        return Q, P, D, Ddag
    
    def configureOperator(self, node, trunc, basis, fmax=4.0):
        if node not in self.getNodeList():
            raise Exception("Node '%i' is not a valid circuit node." % node)
        
        # Get the operator circuit-dependent parameters
        impedance = None
        frequency = None
        update = False
        flux_max = None
        if basis == "oscillator":
            update = True
            index = self.getNodeIndex(node)
            Linv = self.SS.getInverseInductanceMatrix()
            Cinv = self.SS.getInverseCapacitanceMatrix()
            impedance = sy.sqrt(Cinv[index, index]/Linv[index, index])
            frequency = sy.sqrt(Linv[index, index]*Cinv[index, index])
        elif basis == "flux":
            flux_max = fmax
        
        self.operator_data[node] = {
            "truncation": trunc, 
            "basis": basis, 
            "update": update, 
            "impedance": impedance, 
            "frequency": frequency,
            "flux_max": flux_max
        }
    
    ## Expand operator Hilbert spaces and update mapping to associated symbols
    def getExpandedOperatorsMap(self):
        """ Creates all the operators associated with each node in the currently defined circuit. The operators are expanded into the total Hamiltonian Hilbert space.
        
        :return: None
        """
        
        # Get the pos list for indexing the DoFs
        node_list = self.getNodeList()
        
        # Generate the Hilbert space expanders
        Ilist = np.empty([len(node_list)], dtype=np.dtype(qt.Qobj))
        for i, node in enumerate(node_list):
            trunc = self.operator_data[node]["truncation"]
            basis = self.operator_data[node]["basis"]
            if basis == "oscillator":
                Ilist[i] = qt.qeye(trunc)
            elif basis == "charge":
                Ilist[i] = qt.qeye(2*trunc + 1)
        
        # Create mode operators
        Olist = np.empty([len(node_list)], dtype=np.dtype(qt.Qobj))
        op_dict = {}
        for i, node in enumerate(node_list):
            op_dict = {}
            trunc = self.operator_data[node]["truncation"]
            basis = self.operator_data[node]["basis"]
            Q, P, D, Ddag = self.getOperatorList(node)
            
            # Indices minus the current index
            indices = list(range(len(node_list)))
            indices.remove(i)
            
            # Current index is the operator
            Olist[i] = Q
            for j in indices:
                Olist[j] = Ilist[j]
            op_dict["charge"] = qt.tensor(Olist)
            
            Olist[i] = P
            for j in indices:
                Olist[j] = Ilist[j]
            op_dict["flux"] = qt.tensor(Olist)
            
            Olist[i] = D
            for j in indices:
                Olist[j] = Ilist[j]
            op_dict["disp"] = qt.tensor(Olist)
            
            Olist[i] = Ddag
            for j in indices:
                Olist[j] = Ilist[j]
            op_dict["disp_adj"] = qt.tensor(Olist)
            
            self.circ_operators[node] = op_dict.copy()
    
    def regenOps(self, pos, params={"basis":"osc", "osc_impedance":50.0}):
        
        # Get the pos list for indexing the DoFs
        node_list = self.getNodeList()
        i = node_list.index(pos)
        
        # Generate the Hilbert space expanders
        Ilist = np.empty([len(node_list)], dtype=np.dtype(qt.Qobj))
        for i, pos in enumerate(node_list):
            trunc, basis, update = self.mode_truncations[pos]
            if basis == "osc":
                Ilist[i] = qt.qeye(trunc)
            elif basis == "charge":
                Ilist[i] = qt.qeye(2*trunc+1)
        
        # Indices minus the current index
        indices = list(range(len(node_list)))
        indices.remove(i)
        
        # Edit the circuit operators
        op_dict = {}
        Olist = np.empty([len(node_list)], dtype=np.dtype(qt.Qobj))
        trunc, basis, update = self.mode_truncations[pos]
        Q, P, D, Ddag = self.getOperatorList(trunc, **params)
        
        Olist[i] = Q
        for j in indices:
            Olist[j] = Ilist[j]
        op_dict["charge"] = qt.tensor(Olist)
        
        Olist[i] = P
        for j in indices:
            Olist[j] = Ilist[j]
        op_dict["flux"] = qt.tensor(Olist)
        
        Olist[i] = D
        for j in indices:
            Olist[j] = Ilist[j]
        op_dict["disp"] = qt.tensor(Olist)
        
        Olist[i] = Ddag
        for j in indices:
            Olist[j] = Ilist[j]
        op_dict["disp_adj"] = qt.tensor(Olist)
        
        self.circ_operators[pos] = op_dict.copy()
    
    def regenOp(self, pos, params={"basis":"osc", "osc_impedance":50.0}):
        
        # Get the pos list for indexing the DoFs
        node_list = self.getNodeList()
        i = node_list.index(pos)
        
        # Generate the Hilbert space expanders
        Ilist = np.empty([len(node_list)], dtype=np.dtype(qt.Qobj))
        for i, pos in enumerate(node_list):
            trunc, basis, update = self.mode_truncations[pos]
            if basis == "osc":
                Ilist[i] = qt.qeye(trunc)
            elif basis == "charge":
                Ilist[i] = qt.qeye(2*trunc+1)
        
        # Indices minus the current index
        indices = list(range(len(node_list)))
        indices.remove(i)
        
        # Edit the circuit operators
        op_dict = {}
        Olist = np.empty([len(node_list)], dtype=np.dtype(qt.Qobj))
        trunc, basis, update = self.mode_truncations[pos]
        Q, P, D, Ddag = self.getOperatorList(trunc, **params)
        
        Olist[i] = Q
        for j in indices:
            Olist[j] = Ilist[j]
        op_dict["charge"] = qt.tensor(Olist)
        
        Olist[i] = P
        for j in indices:
            Olist[j] = Ilist[j]
        op_dict["flux"] = qt.tensor(Olist)
        
        Olist[i] = D
        for j in indices:
            Olist[j] = Ilist[j]
        op_dict["disp"] = qt.tensor(Olist)
        
        Olist[i] = Ddag
        for j in indices:
            Olist[j] = Ilist[j]
        op_dict["disp_adj"] = qt.tensor(Olist)
        
        self.circ_operators[pos] = op_dict.copy()
    
    ###################################################################################################################
    #       Hamiltonian Building Functions
    ###################################################################################################################
    
    def applyTransformation(self, tmat):
        if type(tmat) != np.matrix:
            raise Exception("transformation matrix should be of type numpy.matrix")
        self.Rmat = tmat
        self.RmatT = tmat.T
        self.Rmatinv = np.linalg.inv(self.Rmat)
        self.RmatTinv = np.linalg.inv(self.Rmat.T)
    
    def getChargeOpVector(self):
        pos = self.getNodeList()
        pos_dofs = self.SS.getDoFSymbolList("charge")
        pos_ops = {k: v["charge"] for k, v in self.circ_operators.items()}

        # Reverse symbol map
        pos_dofs_rev = {v: k for k, v in pos_dofs.items()}
        arr = []
        for sym in self.SS.getChargeVector():
            pos = pos_dofs_rev[sym]
            arr.append(pos_ops[pos])
        self.Qnp = self._init_qobj_vector(arr, dtype=object)
    
    def getFluxOpVector(self):
        pos = self.getNodeList()
        pos_dofs = self.SS.getDoFSymbolList("flux")
        pos_ops = {k: v["flux"] for k, v in self.circ_operators.items()}

        # Reverse symbol map
        pos_dofs_rev = {v: k for k, v in pos_dofs.items()}
        arr = []
        for sym in self.SS.getFluxVector():
            pos = pos_dofs_rev[sym]
            arr.append(pos_ops[pos])
        self.Pnp = self._init_qobj_vector(arr, dtype=object)
    
    def getRightDispOpVector(self):
        pos = self.getNodeList()
        pos_dofs1 = self.SS.getDoFSymbolList("disp")
        pos_dofs2 = self.SS.getDoFSymbolList("disp_adj")
        pos_ops1 = {k:v["disp"] for k, v in self.circ_operators.items()}
        pos_ops2 = {k:v["disp_adj"] for k, v in self.circ_operators.items()}

        # Reverse symbol map
        pos_dofs_rev1 = {v:k for k, v in pos_dofs1.items()}
        pos_dofs_rev2 = {v:k for k, v in pos_dofs2.items()}
        arr = []
        for sym in self.SS.getRightDisplacementOpVector(adjoint=False):
            if sym == 1:
                arr.append(1.0)
                continue
            try:
                pos = pos_dofs_rev1[sym]
                arr.append(pos_ops1[pos])
            except KeyError:
                pos = pos_dofs_rev2[sym]
                arr.append(pos_ops2[pos])
        self.Dr = self._init_qobj_vector(arr, dtype=object)
    
    def getRightDispAdjOpVector(self):
        pos = self.getNodeList()
        pos_dofs1 = self.SS.getDoFSymbolList("disp_adj")
        pos_dofs2 = self.SS.getDoFSymbolList("disp")
        pos_ops1 = {k:v["disp_adj"] for k, v in self.circ_operators.items()}
        pos_ops2 = {k:v["disp"] for k, v in self.circ_operators.items()}

        # Reverse symbol map
        pos_dofs_rev1 = {v:k for k, v in pos_dofs1.items()}
        pos_dofs_rev2 = {v:k for k, v in pos_dofs2.items()}
        arr = []
        for sym in self.SS.getRightDisplacementOpVector(adjoint=True):
            if sym == 1:
                arr.append(1.0)
                continue
            try:
                pos = pos_dofs_rev1[sym]
                arr.append(pos_ops1[pos])
            except KeyError:
                pos = pos_dofs_rev2[sym]
                arr.append(pos_ops2[pos])
        self.Dr_adj = self._init_qobj_vector(arr, dtype=object)
    
    def getLeftDispOpMatrix(self):
        pos = self.getNodeList()
        pos_dofs1 = self.SS.getDoFSymbolList("disp")
        pos_dofs2 = self.SS.getDoFSymbolList("disp_adj")
        pos_ops1 = {k: v["disp"] for k, v in self.circ_operators.items()}
        pos_ops2 = {k: v["disp_adj"] for k, v in self.circ_operators.items()}

        # Reverse symbol map
        pos_dofs_rev1 = {v: k for k, v in pos_dofs1.items()}
        pos_dofs_rev2 = {v: k for k, v in pos_dofs2.items()}
        arr = []
        for sym in self.SS.getLeftDisplacementOpMatrix(adjoint=False, as_vec=True):
            if sym == 1:
                arr.append(1.0)
                continue
            try:
                pos = pos_dofs_rev1[sym]
                arr.append(pos_ops1[pos])
            except KeyError:
                pos = pos_dofs_rev2[sym]
                arr.append(pos_ops2[pos])
        self.Dl = np.asmatrix(np.diag(np.array(arr, dtype=object)))
    
    def getLeftDispAdjOpMatrix(self):
        pos = self.getNodeList()
        pos_dofs1 = self.SS.getDoFSymbolList("disp_adj")
        pos_dofs2 = self.SS.getDoFSymbolList("disp")
        pos_ops1 = {k: v["disp_adj"] for k, v in self.circ_operators.items()}
        pos_ops2 = {k: v["disp"] for k, v in self.circ_operators.items()}

        # Reverse symbol map
        pos_dofs_rev1 = {v: k for k, v in pos_dofs1.items()}
        pos_dofs_rev2 = {v: k for k, v in pos_dofs2.items()}
        arr = []
        for sym in self.SS.getLeftDisplacementOpMatrix(adjoint=True, as_vec=True):
            if sym == 1:
                arr.append(1.0)
                continue
            try:
                pos = pos_dofs_rev1[sym]
                arr.append(pos_ops1[pos])
            except KeyError:
                pos = pos_dofs_rev2[sym]
                arr.append(pos_ops2[pos])
        self.Dl_adj = np.asmatrix(np.diag(np.array(arr, dtype=object)))
    
    def getOpVectors(self):
        self.getChargeOpVector()
        self.getFluxOpVector()
        self.getRightDispOpVector()
        self.getRightDispAdjOpVector()
        self.getLeftDispOpMatrix()
        self.getLeftDispAdjOpMatrix()
    
    def prepareOperators(self):
        
        # Generate symbolics
        #self.getSymbolicOscillatorParams()
        self.getSymbolicExpressions()
        
        # Generate numericals
        self.getExpandedOperatorsMap()
        self.getOpVectors()
    
    def getSymbolicExpressionsOld(self):
        
        # Check for parameterisations
        subs = dict([(self.all_params_sym[k], v) for k, v in self.params_subs.items()])
        
        # FIXME: None of this will work when coordinate transformations are applied
        # Generate final symbolic expressions
        self.Cinv = self.Rmat*self.SS.getInverseCapacitanceMatrix().subs(subs)*self.RmatT
        self.Linv = self.RmatTinv*self.SS.getInverseInductanceMatrix().subs(subs)*self.Rmatinv

        # Get branch inverse inductance matrix for branch current calculations
        self.Linv_b = self.SS.getInverseInductanceMatrix(mode='branch').subs(subs)
        self.Cinv_n = self.SS.getInverseCapacitanceMatrix().subs(subs)
        
        # Symbolic expressions independent of a coupled subsystem
        self.Jvec = self.SS.getJosephsonVector().subs(subs)
        self.Qb = self.SS.getChargeBiasVector().subs(subs)
        self.Pb = self.SS.getFluxBiasVector(mode="branch").subs(subs)
        self.Pbm = self.SS.getFluxBiasMatrix(mode="branch").subs(subs)
    
    def getSymbolicExpressions(self):
        
        # Generate final symbolic expressions
        self.Cinv = self.Rmat*self.SS.getInverseCapacitanceMatrix()*self.RmatT
        self.Linv = self.RmatTinv*self.SS.getInverseInductanceMatrix()*self.Rmatinv

        # Get branch inverse inductance matrix for branch current calculations
        self.Linv_b = self.SS.getInverseInductanceMatrix(mode='branch')
        self.Cinv_n = self.SS.getInverseCapacitanceMatrix()
        
        # Symbolic expressions independent of a coupled subsystem
        self.Jvec = self.SS.getJosephsonVector()
        self.Qb = self.SS.getChargeBiasVector()
        self.Pb = self.SS.getFluxBiasVector(mode="branch")
        self.Pbm = self.SS.getFluxBiasMatrix(mode="branch")
    
    ###################################################################################################################
    #       Parameterisation Functions
    ###################################################################################################################
    
    def addParameterisation(self, old_param, expr, name_map):
        
        # Register the substitution to perform
        self.params_subs[old_param] = expr
        
        # Add the new symbols associated with parameters
        self.all_params_sym.update(name_map)
        
        # Add the new parameters
        for k, v in name_map.items():
            self.params.addParameter(k)
        
        # FIXME: Remove old_param if it does not appear in expr
    
    def rmParameterisation(self, old_param):
        
        # Unregister the substitution
        del self.params_subs[old_param]
    
    def applyParameterisations(self):
        pass
    
    ###################################################################################################################
    #       Numerical Hamiltonian Generation
    ###################################################################################################################
    
    def substituteOld(self, params_dict):
        
        # Set the parameter values
        for k, v in params_dict.items():
            self.params.setParameterValue(k, v)
        
        # If some parameters are omitted from params_dict, those saved in self.params will be used
        subs = {}
        for k, v in self.all_params_sym.items():
            subs[v] = self.params.getParameterValue(k)
        
        # Substitute the resonator parameters if any
        subs = self._get_resonator_substitutions(subs)
        
        # Substitute circuit parameters
        self.Cinvnp = np.asmatrix(self.Cinv.subs(subs), dtype=np.float64)
        self.Linvnp = np.asmatrix(self.Linv.subs(subs), dtype=np.float64)
        self.Jvecnp = np.asmatrix(self.Jvec.subs(subs), dtype=np.float64).T
        
        # Substitute external biases
        self.Qbnp = np.asmatrix(self.Qb.subs(subs), dtype=np.float64) # x 2e
        self.Pbsm = np.asmatrix(self.Pbm.subs(subs), dtype=np.float64)
        self.Pbnp = np.asmatrix(self.Pb.subs(subs), dtype=np.float64) # x Phi0
        
        # Generate exponentiated biases
        # FIXME: This should be removed by expressing the exponentiated biases in 
        # terms of the flux bias symbol
        Pexp1 = []
        Pexp2 = []
        for i in range(self.Pbsm.shape[0]):
            Pexp1.append(np.exp(2j*np.pi*self.Pbsm[i, i]))
            Pexp2.append(np.exp(-2j*np.pi*self.Pbsm[i, i]))
        self.Pexpbnp = np.asmatrix(np.diag(Pexp1), dtype=np.complex64)
        self.Pexpbnpc = np.asmatrix(np.diag(Pexp2), dtype=np.complex64)
        
        # Get branch inverse inductance matrix for branch current calculations
        self.Linvnp_b = np.asmatrix(self.Linv_b.subs(subs), dtype=np.float64)
    
    def substitute(self, params_dict):
        
        # If some parameters are omitted from params_dict, those saved in self.params will be used
        subs = self.SS.getSymbolValuesDict()
        
        # Substitute circuit parameters
        self.Cinvnp = np.asmatrix(self.Cinv.subs(subs), dtype=np.float64)
        self.Linvnp = np.asmatrix(self.Linv.subs(subs), dtype=np.float64)
        self.Jvecnp = np.asmatrix(self.Jvec.subs(subs), dtype=np.float64).T
        
        # Substitute external biases
        self.Qbnp = np.asmatrix(self.Qb.subs(subs), dtype=np.float64) # x 2e
        self.Pbsm = np.asmatrix(self.Pbm.subs(subs), dtype=np.float64)
        self.Pbnp = np.asmatrix(self.Pb.subs(subs), dtype=np.float64) # x Phi0
        
        # Generate exponentiated biases
        Pexp1 = []
        Pexp2 = []
        for i in range(self.Pbsm.shape[0]):
            Pexp1.append(np.exp(2j*np.pi*self.Pbsm[i, i]))
            Pexp2.append(np.exp(-2j*np.pi*self.Pbsm[i, i]))
        self.Pexpbnp = np.asmatrix(np.diag(Pexp1), dtype=np.complex64)
        self.Pexpbnpc = np.asmatrix(np.diag(Pexp2), dtype=np.complex64)
        
        # Get branch inverse inductance matrix for branch current calculations
        self.Linvnp_b = np.asmatrix(self.Linv_b.subs(subs), dtype=np.float64)
    
    def _presubOld(self):
        
        # FIXME: Do we want to use only prepareOperators() to register parameterisations, or do we leave this as it is?
        
        # Check for parameterisations
        p_subs = dict([(self.all_params_sym[k], v) for k, v in self.params_subs.items()])
        
        # Set the parameters that are not being swept
        subs = self._set_non_swept_params()
        
        # Substitute the resonator parameters if any
        #subs = self._get_resonator_substitutions(subs)
        
        # Generate final symbolic expressions
        self.Cinv_pre = self.Rmat*self.SS.getInverseCapacitanceMatrix().subs(p_subs).subs(subs)*self.RmatT
        self.Linv_pre = self.RmatTinv*self.SS.getInverseInductanceMatrix().subs(p_subs).subs(subs)*self.Rmatinv

        # Get branch inverse inductance matrix for branch current calculations
        self.Linv_b_pre = self.SS.getInverseInductanceMatrix(mode='branch').subs(p_subs).subs(subs)
        
        self.Jvec_pre = self.SS.getJosephsonVector().subs(p_subs).subs(subs)
        self.Qb_pre = self.SS.getChargeBiasVector().subs(p_subs).subs(subs)
        self.Pbm_pre = self.SS.getFluxBiasMatrix(mode="branch").subs(p_subs).subs(subs)
        #self.getSymbolicOscillatorParams()
        
        # Find which operators will need to be regenerated for each sweep
        #self._get_regen_ops_list()
    
    def _presub(self):
        # Set the parameters that are not being swept
        subs = self.SS.getNonSweepParametersDict()
        
        # Generate final symbolic expressions
        self.Cinv_pre = self.Rmat*self.SS.getInverseCapacitanceMatrix().subs(subs)*self.RmatT
        self.Linv_pre = self.RmatTinv*self.SS.getInverseInductanceMatrix().subs(subs)*self.Rmatinv

        # Get branch inverse inductance matrix for branch current calculations
        self.Linv_b_pre = self.SS.getInverseInductanceMatrix(mode='branch').subs(subs)
        
        self.Jvec_pre = self.SS.getJosephsonVector().subs(subs)
        self.Qb_pre = self.SS.getChargeBiasVector().subs(subs)
        self.Pbm_pre = self.SS.getFluxBiasMatrix(mode="branch").subs(subs)
        #self.getSymbolicOscillatorParams()
        
        # Find which operators will need to be regenerated for each sweep
        #self._get_regen_ops_list()
    
    def _postsubOld(self, params):
    
        # Set the parameter values
        subs = self._set_swept_params(params)
        
        # Substitute the resonator parameters if any
        #subs = self._get_resonator_substitutions(subs)
        
        # Get the oscillator parameters FIXME
        #self.Znp = dict(zip(self.getNodeList(), [float(self.Z_pre[k].subs(subs))*self.units.getPrefactor("Impe") if self.Z[k] is not None else None for k in self.getNodeList()]))
        #self.wnp = dict(zip(self.getNodeList(), [float(self.w_pre[k].subs(subs))*self.units.getPrefactor("Freq") if self.w[k] is not None else None for k in self.getNodeList()]))
        
        # Regenerate required operators FIXME
        #for k in self.Rlist:
        #    self.regenOp(k, params={"basis":"osc", "osc_impedance":self.Znp[k]})
        
        # Substitute circuit parameters
        self.Cinvnp = np.asmatrix(self.Cinv_pre.subs(subs), dtype=np.float64)
        self.Linvnp = np.asmatrix(self.Linv_pre.subs(subs), dtype=np.float64)
        self.Jvecnp = np.asmatrix(self.Jvec_pre.subs(subs), dtype=np.float64).T
        self.Linvnp_b = np.asmatrix(self.Linv_b_pre.subs(subs), dtype=np.float64)
        
        # Substitute external biases
        self.Qbnp = np.asmatrix(self.Qb_pre.subs(subs), dtype=np.float64) # x 2e
        self.Pbsm = np.asmatrix(self.Pbm_pre.subs(subs), dtype=np.float64)
        #self.Pbnp = np.asmatrix(self.Pb_pre.subs(subs), dtype=np.float64) # x Phi0
        
        # Generate exponentiated biases
        # FIXME: This should be removed by expressing the exponentiated biases in 
        # terms of the flux bias symbol
        Pexp1 = []
        Pexp2 = []
        for i in range(self.Pbsm.shape[0]):
            Pexp1.append(np.exp(2j*np.pi*self.Pbsm[i, i]))
            Pexp2.append(np.exp(-2j*np.pi*self.Pbsm[i, i]))
        self.Pexpbnp = np.asmatrix(np.diag(Pexp1), dtype=np.complex64)
        self.Pexpbnpc = np.asmatrix(np.diag(Pexp2), dtype=np.complex64)
    
    def _postsub(self, params):
    
        # Set the parameter values
        self.SS.setParameterValues(params)
        
        # Get the subs
        subs = self.SS.getSweepParametersDict()
        
        # Substitute circuit parameters
        self.Cinvnp = np.asmatrix(self.Cinv_pre.subs(subs), dtype=np.float64)
        self.Linvnp = np.asmatrix(self.Linv_pre.subs(subs), dtype=np.float64)
        self.Jvecnp = np.asmatrix(self.Jvec_pre.subs(subs), dtype=np.float64).T
        self.Linvnp_b = np.asmatrix(self.Linv_b_pre.subs(subs), dtype=np.float64)
        
        # Substitute external biases
        self.Qbnp = np.asmatrix(self.Qb_pre.subs(subs), dtype=np.float64) # x 2e
        self.Pbsm = np.asmatrix(self.Pbm_pre.subs(subs), dtype=np.float64)
        #self.Pbnp = np.asmatrix(self.Pb_pre.subs(subs), dtype=np.float64) # x Phi0
        
        # Generate exponentiated biases
        Pexp1 = []
        Pexp2 = []
        for i in range(self.Pbsm.shape[0]):
            Pexp1.append(np.exp(2j*np.pi*self.Pbsm[i, i]))
            Pexp2.append(np.exp(-2j*np.pi*self.Pbsm[i, i]))
        self.Pexpbnp = np.asmatrix(np.diag(Pexp1), dtype=np.complex64)
        self.Pexpbnpc = np.asmatrix(np.diag(Pexp2), dtype=np.complex64)
    
    ###################################################################################################################
    #       Evaluables
    ###################################################################################################################
    
    def getHamiltonian(self):
        
        # Get charging energy
        self.Hq = self.units.getPrefactor("Ec")*0.5*\
        util.mdot((self.Qnp + self.Qbnp).T, self.Cinvnp, self.Qnp + self.Qbnp)
        
        # Get flux energy
        self.Hp = self.units.getPrefactor("El")*0.5*\
        util.mdot(self.Pnp.T, self.Linvnp, self.Pnp)
        
        # Get Josephson energy
        self.Hj = -self.units.getPrefactor("Ej")*0.5*\
        (util.mdot(self.Jvecnp, self.Pexpbnpc, self.Dl_adj, self.Dr_adj) + \
         util.mdot(self.Jvecnp, self.Pexpbnp, self.Dl, self.Dr))
        
        # Total Hamiltonian
        self.Ht = (self.Hq+self.Hp+self.Hj)[0, 0]
        return self.Ht
    
    def getBranchCurrents(self):
        # FIXME: This should compute for a single branch per loop that isn't shared in other loops
        # Get edge components
        node_list = self.SS.nodes
        edge_list = self.SS.edges
        
        # Branch currents
        P = self.Pnp #+self.Pbnp
        J = 0.5j*(util.mdot(self.Pexpbnpc, self.Dl_adj, self.Dr_adj) - util.mdot(self.Pexpbnp, self.Dl, self.Dr))
        #R = np.asmatrix(self.SS.getNodeToBranchMatrix(), dtype=np.float64)
        self.Iops = {}
        for i, edge in enumerate(edge_list):
            # Check if edge has an inductor
            if self.SS.CG.isInductiveEdge(edge):
                n1, n2, k = edge
                if n1 > 0:
                    P1 = P[0, node_list.index(n1)]
                else:
                    P1 = 0.0
                if n2 > 0:
                    P2 = P[0, node_list.index(n2)]
                else:
                    P2 = 0.0

                # Take difference of node fluxes of corresponding branch and use *branch* inverse inductance matrix
                #self.Iops[edge] = self.units.getPrefactor("IopL")*(P2-P1)*util.mdot(R, self.Linvnp)[i, i]
                self.Iops[edge] = self.units.getPrefactor("IopL")*(P2 - P1)*self.Linvnp_b[i, i]
            else:
                self.Iops[edge] = self.units.getPrefactor("IopJ")*J.T[0, i]*self.Jvecnp[0, i]
        return self.Iops
    
    def getNodeVoltages(self):
        
        # Get edge components
        node_list = self.SS.nodes
        
        # Node voltages
        Q = self.Qnp + self.Qbnp
        self.Vops = {}
        for i, node in enumerate(node_list):
            self.Vops[node] = self.units.getPrefactor("Vop")*Q[i, 0]*self.Cinvnp[i, i]
        return self.Vops
    
    def getOpExpectationValue(self, op, lket, rket):
        # FIXME: qutip has since fully implemented expectation values in this way
        if type(op) == qt.qobj.Qobj and type(lket) == qt.qobj.Qobj and type(rket) == qt.qobj.Qobj:
            return (lket.dag()*op*rket)[0][0][0]
        elif type(op) in [list, np.ndarray] and type(lket) == qt.qobj.Qobj and type(rket) == qt.qobj.Qobj:
            return np.array([(lket.dag()*opi*rket)[0][0][0] for opi in op])
        elif type(op) in [list, np.ndarray] and type(lket) in [list, np.ndarray] and type(rket) in [list, np.ndarray]:
            return np.array([(lket[i].dag()*op[i]*rket[i])[0][0][0] for i in range(len(op))])
        elif type(op) == qt.qobj.Qobj and type(lket) in [list, np.ndarray] and type(rket) in [list, np.ndarray]:
            return np.array([(lket[i].dag()*op*rket[i])[0][0][0] for i in range(len(lket))])
        else:
            raise Exception("incompatible input types for op (%s), lket (%s) and rket (%s)." % (repr(type(op)), repr(type(lket)), repr(type(rket))))
    
    def getBranchCurrentOperator(self, edge):
        # FIXME: Check edge exists?
        return self.Iops[edge]
    
    def getNodeVoltageOperator(self, node):
        # FIXME: Check node exists?
        return self.Vops[node]
    
    def getSubsystemDerivedParameters(self, subsystem, which=None):
        if which is None:
            return self.subsys_dparams[subsystem]
        else:
            return {k:self.subsys_dparams[subsystem][k] for k in which}
    
    def getSymbolicChargingEnergies(self, node=None):
        # Check for parameterisations
        p_subs = dict([(self.all_params_sym[k], v) for k, v in self.params_subs.items()])
        
        if node is None:
            ret = {}
            for i, pos in enumerate(self.getNodeList()):
                ret[pos] = 0.5 * self.Cinv.subs(p_subs)[i, i] * self.SS.qcp**2
            return ret
        else:
            i = self.getNodeList().index(node)
            return 0.5 * self.Cinv.subs(p_subs)[i, i] * self.SS.qcp**2
    
    def getChargingEnergies(self, node=None):
        if node is None:
            ret = {}
            for i, pos in enumerate(self.getNodeList()):
                ret[pos] = 0.5 * self.Cinvnp[i, i] * self.units.getPrefactor("Ec")
            return ret
        else:
            i = self.getNodeList().index(node)
            return 0.5 * self.Cinvnp[i, i] * self.units.getPrefactor("Ec")
    
    def getFluxEnergies(self, node=None):
        if node is None:
            ret = {}
            for i, pos in enumerate(self.getNodeList()):
                ret[pos] = 0.5 * self.Linvnp[i, i] * self.units.getPrefactor("El")
            return ret
        else:
            i = self.getNodeList().index(node)
            return 0.5 * self.Linvnp[i, i] * self.units.getPrefactor("El")
    
    def getSymbolicJosephsonEnergies(self, edge=None):
        # Check for parameterisations
        p_subs = dict([(self.all_params_sym[k], v) for k, v in self.params_subs.items()])
        
        if edge is None:
            ret = {}
            for i, edge in enumerate(self.SS.edges):
                ret[edge] = self.Jvec.subs(p_subs)[i] * self.SS.phi0/(2*self.SS.pi)
            return ret
        else:
            i = self.SS.edges.index(edge)
            return self.Jvec.subs(p_subs)[i] * self.SS.phi0/(2*self.SS.pi)
    
    def getJosephsonEnergies(self, edge=None):
        if edge is None:
            ret = {}
            for i, edge in enumerate(self.SS.edges):
                ret[edge] = self.Jvecnp[0, i] * self.units.getPrefactor("Ej")
            return ret
        else:
            i = self.SS.edges.index(edge)
            return self.Jvecnp[0, i] * self.units.getPrefactor("Ej")
    
    def getResonatorResponse(self, E, V, nmax=100, cpl_node=None):
        
        # Save the derived parameter values for each sweep value
        if cpl_node is None:
            #for k in self.SS.coupled_subsys[self.subsystem]['derived_parameters'].keys():
            #    self.subsys_dparams[self.subsystem][k].append(self.dpnp[k])
            return None
        
        # Get the model parameters
        gC = self.SS.getParameterValue('g%ir'%cpl_node)
        wrl = self.SS.getParameterValue('f%irl'%cpl_node)
        
        # Get the operator associated with selected node
        index = self.getNodeList().index(cpl_node)
        Op = self.Qnp[index, 0] + self.Qbnp[index, 0]
        
        # Get coupling terms
        E = E - E[0]
        tmax = len(E)
        nmax = nmax + 1 + tmax
        norm = (V[0].dag()*Op*V[1])[0][0][0] # FIXME
        g_list = []
        for i in range(tmax-1):
            g_list.append((V[i].dag()*Op*V[i+1])[0][0][0]) # FIXME
        
        # Apply the circuit derived prefactor
        g_list = gC * np.abs(np.array(g_list)/norm)
        
        # Diagonalise RWA strips
        order = np.zeros(tmax, dtype=np.int)
        diag_bare = np.array([-i*wrl + E[i]
                              for i in range(tmax)])
        eigensolver_order = np.linalg.eigvalsh(np.diag(diag_bare))
        for i in range(tmax):
            index, = np.where(eigensolver_order==diag_bare[i])
            order[i] = index[0]
        Erwa = [0.0]*nmax
        diagonal_elements = None
        offdiagonal_elements = None
        strip_H = None
        e = None
        for n in range(nmax):
            diagonal_elements = np.array([(n-i)*wrl + E[i] for i in range(tmax)])
            offdiagonal_elements = np.array([g_list[i]*np.sqrt((n-i)*(n-i>0)) for i in range(tmax-1)])
            strip_H = (np.diag(diagonal_elements) + np.diag(offdiagonal_elements, 1) +
                       np.diag(offdiagonal_elements, -1))
            e = sc.linalg.eigvalsh(strip_H)
            Erwa[n] = np.array([e[i].real for i in order])
        
        return np.array(Erwa)
    
    def sparsity(self, op):
        return 1 - op.data.nnz/self.getHilbertSpaceSize()**2
    
    ###################################################################################################################
    #       Diagonaliser Configuration
    ###################################################################################################################
    
    def setDiagConfig(self, eigvalues=5, get_vectors=False, sparse=False, sparsesolveropts={"sigma":None, "mode":"normal", "maxiter":None, "tol":1e-3, "which":"SA"}):
        self.diagonalizer_config = {
            'kwargs':{
                'eigvalues':eigvalues, 
                'get_vectors':get_vectors, 
                'sparsesolveropts':sparsesolveropts
            }, 
            'sparse':sparse
        }
        
        # Choose the diagonalizer function and matrix conversion operation
        if sparse:
            self.diagonalizer_config['func'] = util.diagSparseH
        else:
            self.diagonalizer_config['func'] = util.diagDenseH
    
    def getDiagConfig(self):
        return self.diagonalizer_config
    
    def diagonalize(self, M):
        return self.diagonalizer_config['func'](M, **self.diagonalizer_config['kwargs'])
    
    ###################################################################################################################
    #       Parameter Collection Wrapper Functions and Extended Functions
    ###################################################################################################################
    
    ## FIXME: All of these functions should be inherited from Parameters. Parameters should be made to work with the
    ## symbols to avoid having to update both the symbols and the parameters in the present class. Similarly, 
    ## the parameterisations could then be built into the Parameters class. Then we just need that class to have a
    ## getter for the substitutions
    
    ## Create a sweep specification for a single parameter.
    def sweepSpec(self, *args, **kwargs):
        return self.SS.paramSweepSpec(*args, **kwargs)
    
    ## Create an evaluation specification for a single function.
    def evalSpec(self, func, diag, depends, **kwargs):
        return {'eval':func, 'diag':diag, 'depends':depends, 'kwargs':kwargs}
    
    ## Set the value of a parameter.
    def setParameterValue(self, name, value):
        return self.SS.setParameterValue(name, value)
    
    ## Get the value of a parameter.
    def getParameterValue(self, name):
        return self.SS.getParameterValue(name)
    
    ## Set many parameter values.
    def setParameterValues(self, *name_value_pairs):
        self.SS.setParameterValues(*name_value_pairs)
        self.prepareOperators()
        self.substitute({})
    
    ## Get many parameter values.
    def getParameterValues(self, *names):
        return self.SS.getParameterValues(*names)
    
    ## Gets all parameters
    def getParameterValuesDict(self):
        return self.SS.getParameterValuesDict()
    
    def getParameterSweep(self, name):
        return self.SS.getParameterSweep(name)
    
    def getParameterNames(self):
        return self.SS.getParameterNamesList()
    
    def getPrefactor(self, name):
        return self.units.getPrefactor(name)
    
    ###################################################################################################################
    #       Parameter Sweep Functions
    ###################################################################################################################
    
    def paramSweep(self, sweep_spec, eval_spec=[{'eval':'getHamiltonian', 'diag':True, 'depends':None, 'kwargs':{}}], timesweep=False):
        
        # Time initialisation
        if timesweep:
            init_time = time.time()
        
        # Generate sweep grid
        self.SS.ndSweep(sweep_spec)
        
        # FIXME: Determine if we should be saving the data to temp files rather than in RAM:
        # Use the diagonaliser configuration, the requested evaluation functions, and the total number of sweep setpoints that will be used.
        self.__use_temp = True
        if self.__use_temp:
            tmp_results = []
        
        # Do pre-substitutions to avoid repeating un-necessary substitutions in loops
        self._presub()
        
        # FIXME: Check that all symbolic variables have an associated value at this point
        
        # Do the requested evaluations
        if len(eval_spec) > 1:
            
            # Prepare the results structure
            results = {}
            for entry in eval_spec:
                results[entry['eval']] = []
            
            # Time loop
            if timesweep:
                loop_time = time.time()
            for i in range(self.SS.sweep_grid_npts):
                # Do the post-substitutions
                self._postsub(dict([(k, v[i]) for k, v in self.SS.sweep_grid_c.items()]))
                
                # Get requested evaluables
                E = None
                V = None
                for entry in eval_spec:
                    
                    # Check if this evaluable depends on another
                    if entry['depends'] is not None:
                        try:
                            if self.__use_temp:
                                dep = results[entry['depends']]
                            else:
                                dep = results[entry['depends']][i]
                        except:
                            raise Exception("eval spec with 'depends':'%s' entry should be specified after the one it depends on ('%s'), or Possibly invalid 'depends' value." % (entry['depends'], entry['eval'])) # FIXME
                        
                        # In almost every case the depends will be on the eigenvalues and eigenvectors of the independent eval spec
                        try:
                            E, V = dep
                        except:
                            raise Exception("need eigenvectors for 'depends'")
                    
                    # Check if evaluation depends on eigenvalues and eigenvectors
                    if V is not None:
                        M = getattr(self, entry['eval'])(E, V, **entry['kwargs'])
                    else:
                        M = getattr(self, entry['eval'])(**entry['kwargs'])
                    
                    # Check if diagonalisation is required
                    if self.__use_temp:
                        if entry['diag']:
                            results[entry['eval']] = self.diagonalize(M)
                        else:
                            results[entry['eval']] = M
                    else:
                        if entry['diag']:
                            results[entry['eval']].append(self.diagonalize(M))
                        else:
                            results[entry['eval']].append(M)
                    E = None
                    V = None
                
                if self.__use_temp:
                    # Write to temp file
                    f = self.writePart(results)
                    tmp_results.append(f)
            
            # Convert the result entries to ndarray
            if not self.__use_temp:
                for entry in eval_spec:
                    results[entry['eval']] = np.array(results[entry['eval']])
        
        # Do the single requested evaluation
        else:
            results = []
            entry = eval_spec[0]
            
            # Time loop
            if timesweep:
                loop_time = time.time()
            for i in range(self.SS.sweep_grid_npts):
                # Do the post-substitutions
                self._postsub(dict([(k, v[i]) for k, v in self.SS.sweep_grid_c.items()]))
                
                # Get requested evaluable
                M = getattr(self, entry['eval'])(**entry['kwargs'])
                if self.__use_temp:
                    if entry['diag']:
                        results = self.diagonalize(M)
                    else:
                        results = M
                else:
                    if entry['diag']:
                        results.append(self.diagonalize(M))
                    else:
                        results.append(M)
                
                if self.__use_temp:
                    # Write to temp file
                    f = self.writePart(results)
                    tmp_results.append(f)
            
            # Convert results to ndarray
            if not self.__use_temp:
                results = np.array(results)
        
        # Report timings
        if timesweep:
            end_time = time.time()
            print ("Parameter Sweep Duration:")
            print ("  Initialization:\t%.3f s" % (loop_time-init_time))
            print ("  Loop duration:\t%.3f s" % (end_time-loop_time))
            print ("  Avg iteration:\t%.3f s" % ((end_time-loop_time)/self.SS.sweep_grid_npts))
        if self.__use_temp:
            return tmp_results
        else:
            return results
        
    
    def paramSweepFunc(self, sweep_spec, ufcn, ufcn_args={}, get_vectors=False, sparse=False, sparselevels=6, sparsesolveropts={"sigma":None, "mode":"normal", "maxiter":None, "tol":1e-3, "which":"SA"}):
        self.params.ndSweep(sweep_spec)
        #self.eval_dp = True # Signal we want to calculate derived parameters
        
        # FIXME: Need a way to check if the Hamiltonian will need to be updated, this complicates things.
        
        self._presub()
        result = []
        func_result = []
        params = None
        E = None
        V = None
        if not get_vectors:
            if not sparse:
                for i in range(self.params.sweep_grid_npts):
                    params = dict([(k, v[i]) for k, v in self.params.sweep_grid_c.items()])
                    self._postsub(params)
                    E = self.getHamiltonian().eigenenergies()
                    result.append(E)
                    func_result.append(ufcn(self, params, E, **ufcn_args))
            else:
                H = None
                for i in range(self.params.sweep_grid_npts):
                    params = dict([(k, v[i]) for k, v in self.params.sweep_grid_c.items()])
                    self._postsub(params)
                    
                    # Solve
                    E = sc.sparse.linalg.eigsh(self.getHamiltonian().data, k=sparselevels, return_eigenvectors=False, **sparsesolveropts)
                    
                    # Sort the results
                    E.sort()
                    result.append(E)
                    func_result.append(ufcn(self, params, E, **ufcn_args))
        else:
            if not sparse:
                for i in range(self.params.sweep_grid_npts):
                    params = dict([(k, v[i]) for k, v in self.params.sweep_grid_c.items()])
                    self._postsub(params)
                    E, V = self.getHamiltonian().eigenstates()
                    result.append([E, V])
                    func_result.append(ufcn(self, params, E, V, **ufcn_args))
            else:
                H = None
                for i in range(self.params.sweep_grid_npts):
                    params = dict([(k, v[i]) for k, v in self.params.sweep_grid_c.items()])
                    self._postsub(params)
                    
                    # Solve
                    E, V = sc.sparse.linalg.eigsh(self.getHamiltonian().data, k=sparselevels, return_eigenvectors=True, **sparsesolveropts)
                    
                    # FIXME: Sort the results and convert eigenvectors to qutip 
                    result.append((E, V.T))
                    func_result.append(ufcn(self, params, E, V.T, **ufcn_args))
                    
        return np.array(result), np.array(func_result)
    
    def getSweep(self, data, ind_var, static_vars, evaluable="getHamiltonian"):
            return self.SS.getSweepResult(ind_var, static_vars, data=data, key=evaluable)
    
    ###################################################################################################################
    #       Internal Functions
    ###################################################################################################################
    
    # Replaces np asmatrix
    def _init_qobj_vector(self, obj_list, dtype=None):
        obj = np.empty((len(obj_list), 1) , dtype=dtype)
        for i, op in enumerate(obj_list):
            obj[i, 0] = op
        return obj
    
    ## Get list of swept and non-swept parameters
    def _separate_sweep_params(self):
        swept = []
        non_swept = []
        
        # Check which parameters are not being swept
        for k, v in self.all_params_sym.items():
            if k not in self.params.sweep_grid_params:
                non_swept.append(k)
            else:
                swept.append(k)
        return swept, non_swept
    
    ## Set non-swept parameters and return substitutions dict
    def _set_non_swept_params(self):
        subs = {}
        for k, v in self.all_params_sym.items():
            if k not in self.params.sweep_grid_params:
                subs[v] = self.params.getParameterValue(k)
        
        # Attempt to get resonators parameters
        subs = self._get_resonator_substitutions(subs)
        return subs
    
    ## Set swept parameters and return substitutions dict
    def _set_swept_params(self, params):
        subs = {}
        for k, v in params.items():
            subs[self.all_params_sym[k]] = v
            self.params.setParameterValue(k, v)
        
        # Attempt to get resonators parameters
        subs = self._get_resonator_substitutions(subs)
        return subs
    
    ## Find which operators will need to be regenerated for each sweep
    def _get_regen_ops_list(self):
        self.Rlist = []
        for k, v in self.Z_pre.items():
            if v is None:
                continue
            try:
                r = float(v)
            except TypeError:
                self.Rlist.append(k)
    
    def _set_parameter_units(self):
        
        # Get the unit prefactors
        Uf = self.units.getUnitPrefactor('Hz')
        Uo = self.units.getUnitPrefactor('Ohm')
        Uc = self.units.getUnitPrefactor('F')
        Ul = self.units.getUnitPrefactor('H')
        
        # Resonators
        if self.SS._has_resonators():
            for node, resonator in self.SS.CG.resonators_cap.items():
                if resonator is not None:
                    self.SS.addParameterisationPrefactor(resonator["Cr"], 1/(Uf*Uo*Uc))
                    self.SS.addParameterisationPrefactor(resonator["Lr"], Uo/(Uf*Ul))
                    self.SS.addParameterisationPrefactor(resonator["gC"], self.units.getPrefactor('ChgOscCpl'))
                    self.SS.addParameterisationPrefactor(resonator["frl"], self.units.getPrefactor('Freq'))
                    self.SS.addParameterisationPrefactor(resonator["Zrl"], self.units.getPrefactor('Impe'))
        
    
    ## Substitute the correct resonator parameters
    def _get_resonator_substitutions(self, subs):
        
        # Check there are any resonators
        # FIXME: Do this outside the function to avoid the extra copy
        if self.SS.resonator_symbols_cap == {}:
            return subs
        
        # Get the unit prefactors
        Uf = self.units.getUnitPrefactor('Hz')
        Uo = self.units.getUnitPrefactor('Ohm')
        Uc = self.units.getUnitPrefactor('F')
        Ul = self.units.getUnitPrefactor('H')
        
        # Process the resonators
        for node, resonator in self.SS.resonator_symbols_cap.items():
            # Is the frequency and impedance set?
            if resonator['fr'] in subs.keys() and resonator['Zr'] in subs.keys():
                fr = subs[resonator['fr']]
                Zr = subs[resonator['Zr']]
                if fr != 0.0 and Zr != 0.0:
                    # Calculate the resonator component values
                    subs[resonator['Cr']] = 0.5/(np.pi * fr*Uf * Zr*Uo)/Uc
                    subs[resonator['Lr']] = 0.5 * Zr*Uo/(np.pi * fr*Uf)/Ul
                    self.params.setParameterValue(self.SS.CG.resonators_cap[node]['Cr'], subs[resonator['Cr']])
                    self.params.setParameterValue(self.SS.CG.resonators_cap[node]['Lr'], subs[resonator['Lr']])
                    
                    # Calculate coupling and loaded term
                    gC = float(self.SS.resonator_symbols_expr[node]['gC'].subs(subs))*self.units.getPrefactor('ChgOscCpl')
                    frl = float(self.SS.resonator_symbols_expr[node]['frl'].subs(subs))*self.units.getPrefactor('Freq')
                    Zrl = float(self.SS.resonator_symbols_expr[node]['Zrl'].subs(subs))*self.units.getPrefactor('Impe')
                    subs[resonator['gC']] = gC
                    subs[resonator['frl']] = frl
                    subs[resonator['Zrl']] = Zrl
                    self.params.setParameterValue(self.SS.CG.resonators_cap[node]['gC'], subs[resonator['gC']])
                    self.params.setParameterValue(self.SS.CG.resonators_cap[node]['frl'], subs[resonator['frl']])
                    self.params.setParameterValue(self.SS.CG.resonators_cap[node]['Zrl'], subs[resonator['Zrl']])
                else:
                    continue # Let errors be thrown further away
                    #raise Exception("Incorrect parameters for resonator on node %i. fr/Zr combinations must be non-zero." % node)
            #else:
                # Do not allow arbitrary combinations (yet)
            #    raise Exception("Incorrect parameters for resonator on node %i. fr/Zr combinations must be supplied." % node)
        return subs




















