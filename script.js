// Grab the input fields
var billInput = document.getElementById('bill');
var tipInput = document.getElementById('tip');
var peopleInput = document.getElementById('people');

// Grab the result display spots
var tipAmountDisplay = document.getElementById('tip-amount');
var totalDisplay = document.getElementById('total');
var perPersonDisplay = document.getElementById('per-person');

// Run calculate whenever the user changes any input
billInput.addEventListener('input', calculate);
tipInput.addEventListener('input', calculate);
peopleInput.addEventListener('input', calculate);

function calculate() {
  var bill = parseFloat(billInput.value);
  var tipPercent = parseFloat(tipInput.value);
  var people = parseInt(peopleInput.value);

  // If any field is empty or invalid, reset the results
  if (!bill || !tipPercent || !people || people < 1) {
    tipAmountDisplay.textContent = '$0.00';
    totalDisplay.textContent = '$0.00';
    perPersonDisplay.textContent = '$0.00';
    return;
  }

  var tipAmount = bill * (tipPercent / 100);
  var total = bill + tipAmount;
  var perPerson = total / people;

  tipAmountDisplay.textContent = '$' + tipAmount.toFixed(2);
  totalDisplay.textContent = '$' + total.toFixed(2);
  perPersonDisplay.textContent = '$' + perPerson.toFixed(2);
}
