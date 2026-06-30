function [E, info] = open_circuit_voltage(T_fc, p_H2, p_O2)

if nargin == 0
    T_fc = 298.15;
    p_H2 = 1.0;
    p_O2 = 1.0;
elseif nargin == 1
    p_H2 = 1.0;
    p_O2 = 1.0;
elseif nargin ~= 3
    error('open_circuit_voltage:InvalidInputCount', ...
        'Use zero inputs, one input (T_fc), or three inputs: T_fc, p_H2, p_O2.');
end

validateattributes(T_fc, {'numeric'}, {'real', 'finite', 'positive'}, mfilename, 'T_fc');
validateattributes(p_H2, {'numeric'}, {'real', 'finite', 'positive'}, mfilename, 'p_H2');
validateattributes(p_O2, {'numeric'}, {'real', 'finite', 'positive'}, mfilename, 'p_O2');

sameSize_T_pH2 = isscalar(T_fc) || isscalar(p_H2) || isequal(size(T_fc), size(p_H2));
sameSize_T_pO2 = isscalar(T_fc) || isscalar(p_O2) || isequal(size(T_fc), size(p_O2));
sameSize_pH2_pO2 = isscalar(p_H2) || isscalar(p_O2) || isequal(size(p_H2), size(p_O2));

if ~(sameSize_T_pH2 && sameSize_T_pO2 && sameSize_pH2_pO2)
    error('open_circuit_voltage:SizeMismatch', ...
        'Inputs must be scalars or arrays with compatible sizes.');
end

E0 = 1.229;
T0 = 298.15;
temperature_coeff = -0.85e-3;
nernst_coeff = 4.3085e-5;

E = E0 + temperature_coeff .* (T_fc - T0) ...
    + nernst_coeff .* T_fc .* (log(p_H2) + 0.5 .* log(p_O2));

if nargout > 1
    if coder.target('MATLAB')
        info = struct();
        info.name = 'Reversible PEMFC open-circuit voltage';
        info.equation_label = 'Equation (1)';
        info.equation = ['E = 1.229 - 0.85e-3 * (T_fc - 298.15) + ' ...
            '4.3085e-5 * T_fc * (log(p_H2) + 0.5 * log(p_O2))'];
        info.inputs = struct('T_fc_units', 'K', 'p_H2_units', 'atm', 'p_O2_units', 'atm');
        info.assumptions = ['Liquid water is the reference reaction product; ' ...
            'the one-input call uses p_H2 = 1 atm and p_O2 = 1 atm by default.'];
        info.constants = struct( ...
            'E0_V', E0, ...
            'T0_K', T0, ...
            'temperature_coeff_V_per_K', temperature_coeff, ...
            'nernst_coeff_V_per_K', nernst_coeff, ...
            'delta_g0_J_per_mol', -237.2e3, ...
            'faraday_constant_C_per_mol', 96485, ...
            'delta_S0_J_per_mol_K', -163.3, ...
            'S0_H2_J_per_mol_K', 130.7, ...
            'S0_O2_J_per_mol_K', 205.15, ...
            'S0_H2O_liquid_J_per_mol_K', 69.95);
    else
        info = 0;
    end
end
end