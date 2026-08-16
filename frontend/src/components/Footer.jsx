import React from 'react';

export default function Footer() {
  return (
    <footer className="app-footer">
      <div className="footer-content">
        <div className="footer-contact">
          <h4>Contact Us</h4>
          <p>📧 <a href="mailto:ram.2010.rahul@gmail.com">ram.2010.rahul@gmail.com</a></p>
          <p>📞 <a href="tel:+917007303310">7007303310</a></p>
        </div>
      </div>
      <div className="footer-bottom">
        <p>&copy; {new Date().getFullYear()} AlgoX. All rights reserved.</p>
      </div>
    </footer>
  );
}
